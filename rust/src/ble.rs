use bluer::{
    Adapter, Address, Device, Session,
};
use serde::{Deserialize, Serialize};
use std::{
    path::PathBuf,
    str::FromStr,
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
            unit_id: "968900".to_string(),
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
        let now = chrono_now_iso();
        let cfg = Config {
            mac_address: mac.to_string(),
            zone_name: zone.to_string(),
            unit_id: if unit_id.is_empty() { "968900".to_string() } else { unit_id.to_string() },
            updated_at: now,
        };
        if let Ok(data) = serde_json::to_string_pretty(&cfg) {
            let _ = std::fs::write(path, data);
        }
    }

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
        let now = current_time_secs();
        {
            let cached = self.cached_state.lock().await;
            let time = *self.cached_state_time.lock().await;
            if !force_refresh && cached.is_some() && (now - time) < 10 {
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
        let mac = self.get_target_mac().await?;
        info!("Applying settings to {mac}: temp={temp:?}, mode={mode:?}, zone={zone:?}");

        self.write_device_gatt(&mac, temp, mode, zone).await?;
        tokio::time::sleep(tokio::time::Duration::from_millis(250)).await;

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

        let mut state = ThermostatState {
            mac_address: mac.to_string(),
            ble_name: device.name().await.ok().flatten(),
            timestamp: current_time_secs(),
            ..Default::default()
        };

        let services = device.services().await.map_err(|e| e.to_string())?;
        for service in services {
            if service.uuid().await.map_err(|e| e.to_string())? == THERMOSTAT_SERVICE_UUID {
                let chars = service.characteristics().await.map_err(|e| e.to_string())?;
                for ch in chars {
                    let uuid = ch.uuid().await.map_err(|e| e.to_string())?;
                    let val = ch.read().await.unwrap_or_default();

                    if uuid == ROOM_TEMPERATURE_CHAR_UUID {
                        state.temperature = decode_temperature(&val);
                    } else if uuid == SET_POINT_CHAR_UUID {
                        state.set_point = decode_set_point(&val);
                    } else if uuid == CELSIUS_CHAR_UUID {
                        state.is_celsius = decode_celsius(&val);
                    } else if uuid == ZONE_ID_CHAR_UUID {
                        state.zone_name = decode_zone_name(&val);
                    } else if uuid == MODE_OPERATION_CHAR_UUID {
                        state.current_mode = decode_current_mode(&val);
                    } else if uuid == AVAILABLE_MODE_CHAR_UUID {
                        state.available_modes = decode_available_modes(&val);
                        state.available_modes_raw_hex = hex_string(&val);
                    } else if uuid == UNIT_ID_CHAR_UUID {
                        state.unit_id = decode_unit_id(&val);
                    }
                }
            }
        }

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

                    if let Some(z) = zone {
                        if uuid == ZONE_ID_CHAR_UUID {
                            let raw = encode_zone_name(z);
                            ch.write(&raw).await.map_err(|e| e.to_string())?;
                            tokio::time::sleep(tokio::time::Duration::from_millis(150)).await;
                        }
                    }

                    if let Some(m) = mode {
                        if uuid == MODE_OPERATION_CHAR_UUID {
                            let raw = encode_current_mode(m)?;
                            ch.write(&raw).await.map_err(|e| e.to_string())?;
                            tokio::time::sleep(tokio::time::Duration::from_millis(150)).await;
                        }
                    }

                    if let Some(t) = temp {
                        if uuid == SET_POINT_CHAR_UUID {
                            let is_c = self.cached_state.lock().await.as_ref().map(|s| s.is_celsius).unwrap_or(false);
                            let raw = encode_set_point(t, is_c);
                            ch.write(&raw).await.map_err(|e| e.to_string())?;
                            tokio::time::sleep(tokio::time::Duration::from_millis(150)).await;
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

        let mut results = Vec::new();
        let device_addresses = adapter.device_addresses().await.map_err(|e| e.to_string())?;
        for addr in device_addresses {
            if let Ok(dev) = adapter.device(addr) {
                let name = dev.name().await.ok().flatten().unwrap_or_else(|| "Coleman-Mach Thermostat".to_string());
                let rssi = dev.rssi().await.ok().flatten();
                results.push(serde_json::json!({
                    "address": addr.to_string(),
                    "name": name,
                    "rssi": rssi,
                }));
            }
        }

        Ok(results)
    }

    pub async fn start_pairing(&self, pin: Option<&str>) -> (bool, String) {
        let mut active = self.pairing_active.lock().await;
        if *active {
            return (false, "Pairing is already in progress.".to_string());
        }
        *active = true;
        *self.pairing_status.lock().await = "scanning".to_string();
        *self.pairing_message.lock().await = format!(
            "Watching for 0xAA advertising mode. Hold UP+DOWN 5s on thermostat. (PIN: {})",
            pin.unwrap_or("968900")
        );

        (true, "Pairing sequence initiated.".to_string())
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
