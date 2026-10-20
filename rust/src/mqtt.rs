use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS};
use serde_json::json;
use std::{sync::Arc, time::Duration};
use tracing::{error, info, warn};

use crate::ble::ThermostatManager;
use crate::protocol::ThermostatState;

pub struct MqttService {
    client: AsyncClient,
    manager: Arc<ThermostatManager>,
    topic_prefix: String,
    discovery_prefix: String,
}

impl MqttService {
    pub async fn start(
        host: String,
        port: u16,
        username: Option<String>,
        password: Option<String>,
        manager: Arc<ThermostatManager>,
    ) -> Result<Arc<Self>, String> {
        let topic_prefix = "breezy_mach".to_string();
        let discovery_prefix = "homeassistant".to_string();
        let client_id = format!("breezy_mach_{}", std::process::id());

        let mut mqttoptions = MqttOptions::new(client_id, host.clone(), port);
        mqttoptions.set_keep_alive(Duration::from_secs(30));
        mqttoptions.set_clean_session(true);

        if let (Some(u), Some(p)) = (username, password) {
            mqttoptions.set_credentials(u, p);
        }

        let (client, mut eventloop) = AsyncClient::new(mqttoptions, 20);

        let service = Arc::new(Self {
            client: client.clone(),
            manager: manager.clone(),
            topic_prefix: topic_prefix.clone(),
            discovery_prefix: discovery_prefix.clone(),
        });

        // Spawn event loop handler
        let s_loop = service.clone();
        tokio::spawn(async move {
            info!("Starting MQTT eventloop for {}:{}...", host, port);
            loop {
                match eventloop.poll().await {
                    Ok(Event::Incoming(Packet::Publish(publish))) => {
                        let topic = publish.topic.clone();
                        let payload = match std::str::from_utf8(&publish.payload) {
                            Ok(s) => s.trim().to_string(),
                            Err(_) => continue,
                        };
                        s_loop.handle_command(&topic, &payload).await;
                    }
                    Ok(Event::Incoming(Packet::ConnAck(_))) => {
                        info!("MQTT connected to broker!");
                        let _ = s_loop.on_connected().await;
                    }
                    Ok(_) => {}
                    Err(e) => {
                        warn!("MQTT connection error ({}), reconnecting...", e);
                        tokio::time::sleep(Duration::from_secs(3)).await;
                    }
                }
            }
        });

        // Spawn periodic status publisher
        let s_pub = service.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs(2)).await;
            loop {
                s_pub.publish_status().await;
                tokio::time::sleep(Duration::from_secs(15)).await;
            }
        });

        Ok(service)
    }

    async fn on_connected(&self) -> Result<(), String> {
        // Publish availability
        let avail_topic = format!("{}/availability", self.topic_prefix);
        let _ = self
            .client
            .publish(&avail_topic, QoS::AtLeastOnce, true, "online")
            .await;

        // Subscribe to set topics
        let set_filter = format!("{}/set/#", self.topic_prefix);
        let _ = self.client.subscribe(&set_filter, QoS::AtLeastOnce).await;

        // Publish Home Assistant auto-discovery configs
        self.publish_ha_discovery().await;

        // Publish current status
        self.publish_status().await;

        Ok(())
    }

    async fn publish_ha_discovery(&self) {
        let mac = self
            .manager
            .get_target_mac()
            .await
            .unwrap_or_else(|_| "00A050D228D1".to_string())
            .replace(":", "");

        let dev_id = format!("breezy_mach_{}", mac.to_lowercase());
        let device_obj = json!({
            "identifiers": [dev_id],
            "name": "Coleman-Mach RV Thermostat",
            "model": "Bluetooth RV Thermostat",
            "manufacturer": "Airxcel / Coleman-Mach",
            "sw_version": "BreezyMach 1.0.0 (Rust)"
        });

        // 1. Climate Entity Discovery
        let climate_topic = format!(
            "{}/climate/{}/config",
            self.discovery_prefix, dev_id
        );
        let climate_payload = json!({
            "name": "RV Thermostat",
            "unique_id": format!("{}_climate", dev_id),
            "device": device_obj,
            "current_temperature_topic": format!("{}/status", self.topic_prefix),
            "current_temperature_template": "{{ value_json.temperature }}",
            "temperature_command_topic": format!("{}/set/temperature", self.topic_prefix),
            "temperature_state_topic": format!("{}/status", self.topic_prefix),
            "temperature_state_template": "{{ value_json.set_point }}",
            "mode_command_topic": format!("{}/set/mode", self.topic_prefix),
            "mode_state_topic": format!("{}/status", self.topic_prefix),
            "mode_state_template": "{{ value_json.ha_mode }}",
            "modes": ["off", "cool", "fan_only"],
            "fan_mode_command_topic": format!("{}/set/fan_mode", self.topic_prefix),
            "fan_mode_state_topic": format!("{}/status", self.topic_prefix),
            "fan_mode_state_template": "{{ value_json.ha_fan_mode }}",
            "fan_modes": ["auto_high", "auto_low", "high", "low"],
            "min_temp": 33,
            "max_temp": 99,
            "temp_step": 1.0,
            "precision": 1.0,
            "temperature_unit": "F",
            "availability_topic": format!("{}/availability", self.topic_prefix),
            "payload_available": "online",
            "payload_not_available": "offline"
        });

        let _ = self
            .client
            .publish(
                &climate_topic,
                QoS::AtLeastOnce,
                true,
                climate_payload.to_string(),
            )
            .await;

        // 2. Room Temperature Sensor Discovery
        let temp_topic = format!("{}/sensor/{}_temperature/config", self.discovery_prefix, dev_id);
        let temp_payload = json!({
            "name": "RV Room Temperature",
            "unique_id": format!("{}_temperature", dev_id),
            "device": device_obj,
            "device_class": "temperature",
            "state_class": "measurement",
            "unit_of_measurement": "°F",
            "state_topic": format!("{}/status", self.topic_prefix),
            "value_template": "{{ value_json.temperature }}",
            "availability_topic": format!("{}/availability", self.topic_prefix)
        });
        let _ = self
            .client
            .publish(&temp_topic, QoS::AtLeastOnce, true, temp_payload.to_string())
            .await;

        info!("Published Home Assistant MQTT Discovery configuration.");
    }

    pub async fn publish_status(&self) {
        if let Ok(state) = self.manager.get_state(false).await {
            let (ha_mode, ha_fan_mode) = to_ha_mode(&state.current_mode);
            let payload = json!({
                "temperature": state.temperature,
                "set_point": state.set_point,
                "current_mode": state.current_mode,
                "ha_mode": ha_mode,
                "ha_fan_mode": ha_fan_mode,
                "is_celsius": state.is_celsius,
                "zone_name": state.zone_name,
                "mac_address": state.mac_address,
                "timestamp": state.timestamp
            });

            let status_topic = format!("{}/status", self.topic_prefix);
            let _ = self
                .client
                .publish(status_topic, QoS::AtLeastOnce, true, payload.to_string())
                .await;
        }
    }

    async fn handle_command(&self, topic: &str, payload: &str) {
        info!("MQTT received command on {}: {}", topic, payload);

        let subtopic = topic.trim_start_matches(&format!("{}/set/", self.topic_prefix));
        match subtopic {
            "temperature" => {
                if let Ok(temp) = payload.parse::<f32>() {
                    let temp_u8 = temp.round() as u8;
                    info!("Setting target temperature to {}° via MQTT", temp_u8);
                    let _ = self.manager.apply_settings(Some(temp_u8), None, None).await;
                    self.publish_status().await;
                }
            }
            "mode" => {
                let ha_mode = payload.to_lowercase();
                let current_state = self.manager.get_state(false).await.ok();
                let current_fan = current_state
                    .as_ref()
                    .map(|s| to_ha_mode(&s.current_mode).1)
                    .unwrap_or("auto_low");

                let target_mode = from_ha_mode(&ha_mode, current_fan);
                info!("Setting mode to '{}' via MQTT", target_mode);
                let _ = self.manager.apply_settings(None, Some(&target_mode), None).await;
                self.publish_status().await;
            }
            "fan_mode" => {
                let ha_fan_mode = payload.to_lowercase();
                let current_state = self.manager.get_state(false).await.ok();
                let current_mode = current_state
                    .as_ref()
                    .map(|s| to_ha_mode(&s.current_mode).0)
                    .unwrap_or("cool");

                let target_mode = from_ha_mode(current_mode, &ha_fan_mode);
                info!("Setting fan mode to '{}' (combined: '{}') via MQTT", ha_fan_mode, target_mode);
                let _ = self.manager.apply_settings(None, Some(&target_mode), None).await;
                self.publish_status().await;
            }
            "raw_mode" => {
                let target_mode = payload.trim().to_uppercase();
                info!("Setting raw mode to '{}' via MQTT", target_mode);
                let _ = self.manager.apply_settings(None, Some(&target_mode), None).await;
                self.publish_status().await;
            }
            _ => {
                warn!("Unknown MQTT set subtopic: {}", subtopic);
            }
        }
    }
}

fn to_ha_mode(mode: &str) -> (&'static str, &'static str) {
    let m = mode.to_uppercase();
    if m.contains("OFF") {
        ("off", "auto_low")
    } else if m.contains("COOL") {
        if m.contains("AUTO") && m.contains("HIGH") {
            ("cool", "auto_high")
        } else if m.contains("AUTO") {
            ("cool", "auto_low")
        } else if m.contains("HIGH") {
            ("cool", "high")
        } else {
            ("cool", "low")
        }
    } else if m.contains("FAN") {
        if m.contains("HIGH") {
            ("fan_only", "high")
        } else {
            ("fan_only", "low")
        }
    } else {
        ("off", "auto_low")
    }
}

fn from_ha_mode(ha_mode: &str, ha_fan_mode: &str) -> String {
    match ha_mode {
        "off" => "OFF".to_string(),
        "fan_only" => {
            if ha_fan_mode.contains("high") {
                "FAN HIGH".to_string()
            } else {
                "FAN LOW".to_string()
            }
        }
        "cool" => match ha_fan_mode {
            "auto_high" => "COOL AUTO HIGH".to_string(),
            "auto_low" => "COOL AUTO LOW".to_string(),
            "high" => "COOL HIGH".to_string(),
            _ => "COOL LOW".to_string(),
        },
        _ => "OFF".to_string(),
    }
}
