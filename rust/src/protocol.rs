use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use uuid::Uuid;

// =============================================================================
// GATT Service and Characteristic UUIDs
// =============================================================================

pub const THERMOSTAT_SERVICE_UUID: Uuid = uuid::uuid!("c9282723-4680-491b-a904-c066fa81061f");
pub const ROOM_TEMPERATURE_CHAR_UUID: Uuid = uuid::uuid!("382b4008-084a-4158-b378-66674091b1e2");
pub const ZONE_ID_CHAR_UUID: Uuid = uuid::uuid!("beb89473-05fe-41a0-9896-2c082660f19a");
pub const SET_POINT_CHAR_UUID: Uuid = uuid::uuid!("09996813-fe7d-48ce-9b47-11634c80263c");
pub const AVAILABLE_MODE_CHAR_UUID: Uuid = uuid::uuid!("0575cdb7-d448-4bff-bc53-3b900d2a829f");
pub const CELSIUS_CHAR_UUID: Uuid = uuid::uuid!("3016e3fc-1dbb-455e-b268-80750e36c950");
pub const MODE_OPERATION_CHAR_UUID: Uuid = uuid::uuid!("9230a9ef-347c-4645-8fd9-cbb830d714bf");
pub const UNIT_ID_CHAR_UUID: Uuid = uuid::uuid!("e21b2b2c-379e-4166-b835-c71ef7eadfdf");

pub const MANUFACTURER_COMPANY_ID: u16 = 0xFFFF;

pub const MODES: &[&str] = &[
    "OFF",
    "FAN LOW",
    "FAN HIGH",
    "COOL LOW",
    "COOL HIGH",
    "COOL AUTO LOW",
    "COOL AUTO HIGH",
    "HEAT",
    "HEAT ELEC",
    "HEAT GAS",
];

// =============================================================================
// Thermostat State Model
// =============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThermostatState {
    pub mac_address: String,
    pub ble_name: Option<String>,
    pub zone_name: String,
    pub unit_id: String,
    pub is_celsius: bool,
    pub temperature: Option<u8>,
    pub set_point: Option<u8>,
    pub current_mode: String,
    pub available_modes: Vec<String>,
    pub available_modes_raw_hex: String,
    pub timestamp: u64,
}

impl Default for ThermostatState {
    fn default() -> Self {
        Self {
            mac_address: String::new(),
            ble_name: None,
            zone_name: String::new(),
            unit_id: String::new(),
            is_celsius: false,
            temperature: None,
            set_point: None,
            current_mode: String::new(),
            available_modes: Vec::new(),
            available_modes_raw_hex: String::new(),
            timestamp: 0,
        }
    }
}

impl ThermostatState {
    pub fn temp_unit(&self) -> &'static str {
        if self.is_celsius { "°C" } else { "°F" }
    }
}

// =============================================================================
// Protocol Codecs
// =============================================================================

pub fn decode_temperature(raw: &[u8]) -> Option<u8> {
    if raw.is_empty() || raw[0] == 0xFF {
        None
    } else {
        Some(raw[0])
    }
}

pub fn decode_set_point(raw: &[u8]) -> Option<u8> {
    if raw.is_empty() || raw[0] == 0xFF {
        None
    } else {
        Some(raw[0])
    }
}

pub fn encode_set_point(set_point: u8, is_celsius: bool) -> Vec<u8> {
    let (lo, hi) = if is_celsius { (1, 37) } else { (33, 99) };
    let clamped = set_point.clamp(lo, hi);
    vec![clamped]
}

pub fn decode_celsius(raw: &[u8]) -> bool {
    if raw.is_empty() {
        false
    } else {
        raw[0] == 1
    }
}

pub fn decode_zone_name(raw: &[u8]) -> String {
    let len = raw.len().min(7);
    let s: String = raw[..len]
        .iter()
        .filter(|&&b| b != 0)
        .map(|&b| b as char)
        .collect();
    s.trim().to_string()
}

pub fn encode_zone_name(name: &str) -> Vec<u8> {
    let clean = name.trim();
    let truncated = if clean.len() > 7 { &clean[..7] } else { clean };
    let padded = format!("{:<7}", truncated);
    padded.as_bytes().to_vec()
}

pub fn decode_current_mode(raw: &[u8]) -> String {
    let len = raw.len().min(14);
    let s: String = raw[..len]
        .iter()
        .filter(|&&b| b != 0)
        .map(|&b| b as char)
        .collect();
    s.trim().to_string()
}

pub fn normalize_mode(mode_input: &str) -> Result<String, String> {
    let cleaned = mode_input.trim();
    let upper = cleaned.to_uppercase().split_whitespace().collect::<Vec<_>>().join(" ");
    
    if MODES.iter().any(|&m| m == upper) {
        return Ok(upper);
    }

    let mut aliases: HashMap<&str, &str> = HashMap::new();
    aliases.insert("off", "OFF");
    aliases.insert("fan-low", "FAN LOW");
    aliases.insert("fan-high", "FAN HIGH");
    aliases.insert("cool-low", "COOL LOW");
    aliases.insert("cool-high", "COOL HIGH");
    aliases.insert("cool-auto-low", "COOL AUTO LOW");
    aliases.insert("cool-auto-high", "COOL AUTO HIGH");
    aliases.insert("heat", "HEAT");
    aliases.insert("heat-elec", "HEAT ELEC");
    aliases.insert("heat-gas", "HEAT GAS");

    if let Some(&canonical) = aliases.get(cleaned.to_lowercase().as_str()) {
        Ok(canonical.to_string())
    } else {
        Err(format!("Invalid mode '{}'. Valid modes: {}", mode_input, MODES.join(", ")))
    }
}

pub fn encode_current_mode(mode_input: &str) -> Result<Vec<u8>, String> {
    let canonical = normalize_mode(mode_input)?;
    Ok(canonical.as_bytes().to_vec())
}

pub fn decode_available_modes(raw: &[u8]) -> Vec<String> {
    // Replica of BLEManager.bin2Hex(byte[])
    let mut res: i32 = 0;
    for &b in raw {
        res = (res << 1) | (b as i8 as i32);
    }

    // Replica of BLEManager.getModesList(int)
    let mut out = Vec::new();
    let mut mask = res;
    let mut idx = (MODES.len() as isize) - 1;

    while mask > 0 && idx >= 0 {
        let bit = mask & 1;
        mask >>= 1;
        if bit == 1 {
            out.push(MODES[idx as usize].to_string());
        }
        idx -= 1;
    }

    out
}

pub fn decode_unit_id(raw: &[u8]) -> String {
    let len = raw.len().min(3);
    let mut out = String::new();
    for &b in &raw[..len] {
        out.push_str(&format!("{:02}", b));
    }
    out
}
