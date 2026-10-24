use bluer::{
    Adapter, Address, Device, Session,
};
use serde::{Deserialize, Serialize};
use std::{
    path::PathBuf,
    str::FromStr,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;
use tracing::info;

use crate::protocol::*;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub mac_address: String,
    pub zone_name: String,
    pub unit_id: String,
    pub updated_at: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mac_address: String::new(),
            zone_name: String::new(),
            unit_id: String::new(),
            updated_at: String::new(),
        }
    }
}

pub struct ThermostatManager {
    target_mac: Mutex<Option<String>>,
    cached_state: Mutex<Option<ThermostatState>>,
    cached_state_time: Mutex<u64>,
    pairing_active: Mutex<bool>,
    pairing_status: Mutex<String>,
    pairing_message: Mutex<String>,
    gatt_lock: Mutex<()>,
}

impl ThermostatManager {
    pub fn new(mac_override: Option<String>) -> Self {
        let mac = mac_override.or_else(|| Self::load_config().map(|c| c.mac_address));
        Self {
            target_mac: Mutex::new(mac),
            cached_state: Mutex::new(None),
            cached_state_time: Mutex::new(0),
            pairing_active: Mutex::new(false),
            pairing_status: Mutex::new("idle".to_string()),
            pairing_message: Mutex::new("Ready".to_string()),
            gatt_lock: Mutex::new(()),
        }
    }

    pub fn config_path() -> PathBuf {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home).join(".config/coleman_mach_ble.json")
    }

    pub fn load_config() -> Option<Config> {
        let path = Self::config_path();
        if path.exists() {
            if let Ok(data) = std::fs::read_to_string(path) {
                return serde_json::from_str(&data).ok();
            }
        }
        None
    }

    pub fn save_config(mac: &str, zone: &str, unit_id: &str) {
        let path = Self::config_path();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let existing = Self::load_config().unwrap_or_default();
        let now = chrono_now_iso();
        let cfg = Config {
            mac_address: if mac.is_empty() { existing.mac_address } else { mac.to_string() },
            zone_name: if zone.is_empty() { existing.zone_name } else { zone.to_string() },
            unit_id: if unit_id.is_empty() { existing.unit_id } else { unit_id.to_string() },
            updated_at: now,
        };
        if let Ok(data) = serde_json::to_string_pretty(&cfg) {
            let _ = std::fs::write(path, data);
        }
    }

    pub const DEFAULT_CACHE_TTL_SECS: u64 = 300;

    pub async fn get_config(&self) -> Config {
        Self::load_config().unwrap_or_default()
    }

    pub async fn get_target_mac(&self) -> Result<String, String> {
        let mac_opt = self.target_mac.lock().await.clone();
        if let Some(m) = mac_opt {
            if !m.is_empty() {
                return Ok(m);
            }
        }
        if let Some(cfg) = Self::load_config() {
            if !cfg.mac_address.is_empty() {
                *self.target_mac.lock().await = Some(cfg.mac_address.clone());
                return Ok(cfg.mac_address);
            }
        }
        Err("No Coleman-Mach thermostat MAC address configured.".to_string())
    }

    pub async fn get_state(&self, force_refresh: bool) -> Result<ThermostatState, String> {
        if *self.pairing_active.lock().await {
            if let Some(cached) = self.cached_state.lock().await.clone() {
                return Ok(cached);
            }
        }

        let now = current_time_secs();
        {
            let cached = self.cached_state.lock().await;
            let time = *self.cached_state_time.lock().await;
            if !force_refresh && cached.is_some() && (now.saturating_sub(time)) < Self::DEFAULT_CACHE_TTL_SECS {
                return Ok(cached.clone().unwrap());
            }
        }

        let _lock = self.gatt_lock.lock().await;
        // Re-check cache after acquiring GATT lock so concurrent callers share a single BLE read
        let now = current_time_secs();
        {
            let cached = self.cached_state.lock().await;
            let time = *self.cached_state_time.lock().await;
            if !force_refresh && cached.is_some() && (now.saturating_sub(time)) < Self::DEFAULT_CACHE_TTL_SECS {
                return Ok(cached.clone().unwrap());
            }
        }

        let mac = self.get_target_mac().await?;
        let state = self.read_device_gatt(&mac).await?;
        
        *self.cached_state.lock().await = Some(state.clone());
        *self.cached_state_time.lock().await = current_time_secs();
        Self::save_config(&state.mac_address, &state.zone_name, &state.unit_id);

        Ok(state)
    }

    pub async fn apply_settings(
        &self,
        temp: Option<u8>,
        mode: Option<&str>,
        zone: Option<&str>,
    ) -> Result<ThermostatState, String> {
        let _lock = self.gatt_lock.lock().await;
        let mac = self.get_target_mac().await?;
        info!("Applying settings to {mac}: temp={temp:?}, mode={mode:?}, zone={zone:?}");

        self.write_device_gatt(&mac, temp, mode, zone).await?;
        tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

        let state = self.read_device_gatt(&mac).await?;
        *self.cached_state.lock().await = Some(state.clone());
        *self.cached_state_time.lock().await = current_time_secs();
        Self::save_config(&state.mac_address, &state.zone_name, &state.unit_id);

        Ok(state)
    }

    async fn connect_device(&self, session: &Session, mac: &str) -> Result<(Adapter, Device), String> {
        let adapter = session.default_adapter().await.map_err(|e| e.to_string())?;
        adapter.set_powered(true).await.map_err(|e| e.to_string())?;

        let addr = Address::from_str(mac).map_err(|e| format!("Invalid MAC {mac}: {e}"))?;
        let device = adapter.device(addr).map_err(|e| e.to_string())?;

        if !device.is_connected().await.unwrap_or(false) {
            info!("Connecting to BLE device {mac}...");
            device.connect().await.map_err(|e| format!("Connection error: {e}"))?;
        }

        Ok((adapter, device))
    }

    async fn read_device_gatt(&self, mac: &str) -> Result<ThermostatState, String> {
        let session = Session::new().await.map_err(|e| e.to_string())?;
        let (_adapter, device) = self.connect_device(&session, mac).await?;

        let prev_cached = self.cached_state.lock().await.clone();
        let saved_cfg = Self::load_config().unwrap_or_default();

        let mut state = ThermostatState {
            mac_address: mac.to_string(),
            ble_name: device.name().await.ok().flatten(),
            zone_name: prev_cached
                .as_ref()
                .map(|s| s.zone_name.clone())
                .filter(|z| !z.is_empty())
                .unwrap_or(saved_cfg.zone_name),
            unit_id: prev_cached
                .as_ref()
                .map(|s| s.unit_id.clone())
                .filter(|u| !u.is_empty())
                .unwrap_or(saved_cfg.unit_id),
            is_celsius: prev_cached.as_ref().map(|s| s.is_celsius).unwrap_or(false),
            temperature: None,
            set_point: prev_cached.as_ref().and_then(|s| s.set_point),
            current_mode: prev_cached.as_ref().map(|s| s.current_mode.clone()).unwrap_or_default(),
            available_modes: prev_cached.as_ref().map(|s| s.available_modes.clone()).unwrap_or_default(),
            available_modes_raw_hex: prev_cached.as_ref().map(|s| s.available_modes_raw_hex.clone()).unwrap_or_default(),
            timestamp: current_time_secs(),
        };

        let services = device.services().await.map_err(|e| e.to_string())?;
        for service in services {
            if service.uuid().await.map_err(|e| e.to_string())? == THERMOSTAT_SERVICE_UUID {
                let chars = service.characteristics().await.map_err(|e| e.to_string())?;
                for ch in chars {
                    let uuid = ch.uuid().await.map_err(|e| e.to_string())?;
                    let Ok(val) = ch.read().await else {
                        continue;
                    };
                    if val.is_empty() {
                        continue;
                    }

                    if uuid == ROOM_TEMPERATURE_CHAR_UUID {
                        state.temperature = decode_temperature(&val);
                    } else if uuid == SET_POINT_CHAR_UUID {
                        if let Some(sp) = decode_set_point(&val) {
                            state.set_point = Some(sp);
                        }
                    } else if uuid == CELSIUS_CHAR_UUID {
                        state.is_celsius = decode_celsius(&val);
                    } else if uuid == ZONE_ID_CHAR_UUID {
                        let z = decode_zone_name(&val);
                        if !z.is_empty() {
                            state.zone_name = z;
                        }
                    } else if uuid == MODE_OPERATION_CHAR_UUID {
                        let m = decode_current_mode(&val);
                        if !m.is_empty() {
                            state.current_mode = m;
                        }
                    } else if uuid == AVAILABLE_MODE_CHAR_UUID {
                        let modes = decode_available_modes(&val);
                        if !modes.is_empty() {
                            state.available_modes = modes;
                            state.available_modes_raw_hex = hex_string(&val);
                        }
                    } else if uuid == UNIT_ID_CHAR_UUID {
                        let uid = decode_unit_id(&val);
                        if !uid.is_empty() {
                            state.unit_id = uid;
                        }
                    }
                }
            }
        }

        // Disconnect cleanly after completing GATT transaction so the thermostat's
        // Microchip BLE module does not hold an idle connection open.
        let _ = device.disconnect().await;

        Ok(state)
    }

    async fn write_device_gatt(
        &self,
        mac: &str,
        temp: Option<u8>,
        mode: Option<&str>,
        zone: Option<&str>,
    ) -> Result<(), String> {
        let session = Session::new().await.map_err(|e| e.to_string())?;
        let (_adapter, device) = self.connect_device(&session, mac).await?;

        let services = device.services().await.map_err(|e| e.to_string())?;
        for service in services {
            if service.uuid().await.map_err(|e| e.to_string())? == THERMOSTAT_SERVICE_UUID {
                let chars = service.characteristics().await.map_err(|e| e.to_string())?;
                for ch in chars {
                    let uuid = ch.uuid().await.map_err(|e| e.to_string())?;

                    let write_req = bluer::gatt::remote::CharacteristicWriteRequest {
                        op_type: bluer::gatt::WriteOp::Request,
                        ..Default::default()
                    };

                    if let Some(z) = zone {
                        if uuid == ZONE_ID_CHAR_UUID {
                            let raw = encode_zone_name(z);
                            ch.write_ext(&raw, &write_req).await.map_err(|e| e.to_string())?;
                            tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
                        }
                    }

                    if let Some(m) = mode {
                        if uuid == MODE_OPERATION_CHAR_UUID {
                            let raw = encode_current_mode(m)?;
                            ch.write_ext(&raw, &write_req).await.map_err(|e| e.to_string())?;
                            tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
                        }
                    }

                    if let Some(t) = temp {
                        if uuid == SET_POINT_CHAR_UUID {
                            let is_c = self.cached_state.lock().await.as_ref().map(|s| s.is_celsius).unwrap_or(false);
                            let raw = encode_set_point(t, is_c);
                            ch.write_ext(&raw, &write_req).await.map_err(|e| e.to_string())?;
                            tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
                        }
                    }
                }
            }
        }

        Ok(())
    }

    pub async fn scan_devices(&self) -> Result<Vec<serde_json::Value>, String> {
        let session = Session::new().await.map_err(|e| e.to_string())?;
        let adapter = session.default_adapter().await.map_err(|e| e.to_string())?;
        adapter.set_powered(true).await.map_err(|e| e.to_string())?;

        // Run a short 2-second discovery to pick up active advertisements
        let _discovery = adapter.discover_devices().await.ok();
        tokio::time::sleep(tokio::time::Duration::from_millis(2000)).await;

        let mut results = Vec::new();
        let device_addresses = adapter.device_addresses().await.map_err(|e| e.to_string())?;
        for addr in device_addresses {
            if let Ok(dev) = adapter.device(addr) {
                let name = dev.name().await.ok().flatten().unwrap_or_else(|| "Coleman-Mach Thermostat".to_string());
                let rssi = dev.rssi().await.ok().flatten();
                let mut adv_mode = "NORMAL_LOCKED (0x00)".to_string();
                if let Ok(Some(mfg)) = dev.manufacturer_data().await {
                    for (_k, v) in mfg {
                        if v.first() == Some(&0xAA) {
                            adv_mode = "PAIRING_OPEN (0xAA)".to_string();
                        }
                    }
                }
                results.push(serde_json::json!({
                    "address": addr.to_string(),
                    "name": name,
                    "rssi": rssi,
                    "adv_mode": adv_mode,
                }));
            }
        }

        Ok(results)
    }

    pub async fn start_pairing(self: &Arc<Self>, pin: Option<String>) -> (bool, String) {
        let mut active = self.pairing_active.lock().await;
        if *active {
            return (false, "Pairing is already in progress.".to_string());
        }
        *active = true;
        *self.pairing_status.lock().await = "scanning".to_string();
        *self.pairing_message.lock().await = "Listening for thermostat in Pairing Mode (0xAA)... Hold UP+DOWN on unit for 5s.".to_string();

        let this = self.clone();
        tokio::spawn(async move {
            let _ = this.run_pairing_flow(pin, 60).await;
        });

        (true, "Pairing sequence initiated. Hold UP+DOWN on thermostat for 5s.".to_string())
    }

    pub async fn run_pairing_flow(&self, pin_opt: Option<String>, timeout_secs: u64) -> Result<ThermostatState, String> {
        let pin_str = pin_opt
            .filter(|p| !p.trim().is_empty())
            .or_else(|| Self::load_config().map(|c| c.unit_id).filter(|u| !u.trim().is_empty()))
            .unwrap_or_default()
            .replace(" ", "")
            .trim()
            .to_string();

        if pin_str.is_empty() || pin_str.len() != 6 || pin_str.parse::<u32>().is_err() {
            let err = "A 6-digit numeric PIN is required for pairing. Enter the PIN flashing on your thermostat LCD (e.g. 123456).".to_string();
            *self.pairing_status.lock().await = "failed".to_string();
            *self.pairing_message.lock().await = err.clone();
            *self.pairing_active.lock().await = false;
            return Err(err);
        }
        let passkey_num: u32 = pin_str.parse().unwrap();

        *self.pairing_active.lock().await = true;
        *self.pairing_status.lock().await = "scanning".to_string();
        *self.pairing_message.lock().await = format!(
            "Listening for thermostat in Pairing Mode (0xAA)... Hold UP+DOWN on unit for 5s. (PIN: {})",
            pin_str
        );

        let session = match Session::new().await {
            Ok(s) => s,
            Err(e) => {
                let err = format!("Failed to create BlueZ D-Bus session: {e}");
                *self.pairing_status.lock().await = "failed".to_string();
                *self.pairing_message.lock().await = err.clone();
                *self.pairing_active.lock().await = false;
                return Err(err);
            }
        };

        let adapter = match session.default_adapter().await {
            Ok(a) => a,
            Err(e) => {
                let err = format!("Failed to get default Bluetooth adapter: {e}");
                *self.pairing_status.lock().await = "failed".to_string();
                *self.pairing_message.lock().await = err.clone();
                *self.pairing_active.lock().await = false;
                return Err(err);
            }
        };

        let _ = adapter.set_powered(true).await;

        // Register BlueZ agent for automatic SMP Passkey response
        let pin_for_agent = pin_str.clone();
        let agent = bluer::agent::Agent {
            request_default: true,
            request_pin_code: Some(Box::new(move |_req| {
                let p = pin_for_agent.clone();
                Box::pin(async move { Ok(p) })
            })),
            request_passkey: Some(Box::new(move |_req| {
                Box::pin(async move { Ok(passkey_num) })
            })),
            request_confirmation: Some(Box::new(move |_req| {
                Box::pin(async move { Ok(()) })
            })),
            request_authorization: Some(Box::new(move |_req| {
                Box::pin(async move { Ok(()) })
            })),
            authorize_service: Some(Box::new(move |_req| {
                Box::pin(async move { Ok(()) })
            })),
            ..Default::default()
        };

        let _agent_handle = match session.register_agent(agent).await {
            Ok(h) => h,
            Err(e) => {
                let err = format!("Failed to register BlueZ passkey agent: {e}");
                *self.pairing_status.lock().await = "failed".to_string();
                *self.pairing_message.lock().await = err.clone();
                *self.pairing_active.lock().await = false;
                return Err(err);
            }
        };

        info!("BlueZ passkey agent registered for PIN: {}", pin_str);

        // Check already known devices first for 0xAA manufacturer advertisement
        let mut target_addr: Option<Address> = None;
        if let Ok(addrs) = adapter.device_addresses().await {
            for addr in addrs {
                if let Ok(dev) = adapter.device(addr) {
                    if let Ok(Some(mfg)) = dev.manufacturer_data().await {
                        for (_k, v) in mfg {
                            if v.first() == Some(&0xAA) {
                                info!("Found known device {} in open pairing mode (0xAA)!", addr);
                                target_addr = Some(addr);
                                break;
                            }
                        }
                    }
                    if target_addr.is_some() {
                        break;
                    }
                }
            }
        }

        // If not found in known devices, start active BLE discovery and wait
        if target_addr.is_none() {
            info!("Starting BLE discovery to find thermostat advertising 0xAA...");
            let start = tokio::time::Instant::now();
            let timeout = tokio::time::Duration::from_secs(timeout_secs);

            // Start discovery session
            let _discovery = adapter.discover_devices().await.ok();

            while start.elapsed() < timeout {
                if let Ok(addrs) = adapter.device_addresses().await {
                    for addr in addrs {
                        if let Ok(dev) = adapter.device(addr) {
                            if let Ok(Some(mfg)) = dev.manufacturer_data().await {
                                for (_k, v) in mfg {
                                    if v.first() == Some(&0xAA) {
                                        info!("Detected device {} in pairing mode (0xAA)!", addr);
                                        target_addr = Some(addr);
                                        break;
                                    }
                                }
                            }
                            if target_addr.is_some() {
                                break;
                            }
                        }
                    }
                }
                if target_addr.is_some() {
                    break;
                }
                tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
            }
        }

        // Fallback: If still None, check if we have an existing configured MAC
        if target_addr.is_none() {
            if let Ok(cfg_mac) = self.get_target_mac().await {
                if let Ok(addr) = Address::from_str(&cfg_mac) {
                    info!("No new 0xAA detected, using configured MAC address {}", addr);
                    target_addr = Some(addr);
                }
            }
        }

        let addr = match target_addr {
            Some(a) => a,
            None => {
                let err = format!("Timeout: Did not detect pairing advertisement (0xAA) within {}s.", timeout_secs);
                *self.pairing_status.lock().await = "failed".to_string();
                *self.pairing_message.lock().await = err.clone();
                *self.pairing_active.lock().await = false;
                return Err(err);
            }
        };

        *self.pairing_status.lock().await = "connecting".to_string();
        *self.pairing_message.lock().await = format!("Detected {}! Bonding with PIN {}...", addr, pin_str);

        let dev = match adapter.device(addr) {
            Ok(d) => d,
            Err(e) => {
                let err = format!("Failed to get device {}: {e}", addr);
                *self.pairing_status.lock().await = "failed".to_string();
                *self.pairing_message.lock().await = err.clone();
                *self.pairing_active.lock().await = false;
                return Err(err);
            }
        };

        // Acquire GATT lock for pairing/bonding & reading
        let _lock = self.gatt_lock.lock().await;

        // Attempt BlueZ pairing / bonding
        match dev.pair().await {
            Ok(_) => info!("BlueZ device.pair() succeeded for {}", addr),
            Err(e) => info!("BlueZ device.pair() notice for {}: {}", addr, e),
        }
        let _ = dev.set_trusted(true).await;

        if !dev.is_connected().await.unwrap_or(false) {
            let _ = dev.connect().await;
        }

        // Read GATT characteristics with retries
        let mut final_state = None;
        let mut last_err = String::new();
        for attempt in 1..=5 {
            tokio::time::sleep(tokio::time::Duration::from_millis(600)).await;
            match self.read_device_gatt(&addr.to_string()).await {
                Ok(s) => {
                    final_state = Some(s);
                    break;
                }
                Err(e) => {
                    info!("Read GATT attempt {} on {} failed: {}", attempt, addr, e);
                    last_err = e;
                }
            }
        }

        match final_state {
            Some(state) => {
                Self::save_config(&addr.to_string(), &state.zone_name, &state.unit_id);
                *self.target_mac.lock().await = Some(addr.to_string());
                *self.cached_state.lock().await = Some(state.clone());
                *self.cached_state_time.lock().await = current_time_secs();

                let msg = format!(
                    "Successfully paired with {} (Zone: '{}', Setpoint: {}{})!",
                    addr,
                    state.zone_name,
                    state.set_point.unwrap_or(0),
                    state.temp_unit()
                );
                *self.pairing_status.lock().await = "paired".to_string();
                *self.pairing_message.lock().await = msg;
                *self.pairing_active.lock().await = false;
                Ok(state)
            }
            None => {
                let err = format!("Failed to read thermostat characteristics after bonding: {}", last_err);
                *self.pairing_status.lock().await = "failed".to_string();
                *self.pairing_message.lock().await = err.clone();
                *self.pairing_active.lock().await = false;
                Err(err)
            }
        }
    }

    pub async fn get_pairing_status(&self) -> (bool, String, String) {
        (
            *self.pairing_active.lock().await,
            self.pairing_status.lock().await.clone(),
            self.pairing_message.lock().await.clone(),
        )
    }
}

fn current_time_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn chrono_now_iso() -> String {
    let now = current_time_secs();
    format!("{now}")
}

fn hex_string(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02X}")).collect()
}
