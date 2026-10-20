mod ble;
mod mqtt;
mod protocol;
mod server;

use clap::{Parser, Subcommand};
use std::sync::Arc;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

use ble::ThermostatManager;
use protocol::ThermostatState;

#[derive(Parser)]
#[command(name = "breezy-mach")]
#[command(author = "ChiefTechnicalWizard <chieftechnicalwizard@gmail.com>")]
#[command(version = "1.0.0")]
#[command(about = "Compatible Engineered Bluetooth controller and local web dashboard for Coleman-Mach RV thermostats")]
struct Cli {
    #[arg(long, global = true, help = "Thermostat Bluetooth MAC address override")]
    mac: Option<String>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    #[command(about = "Start the embedded web dashboard and REST API service")]
    Serve {
        #[arg(long, default_value = "0.0.0.0", help = "HTTP host to bind")]
        host: String,

        #[arg(long, default_value_t = 8050, help = "HTTP port to bind")]
        port: u16,

        #[arg(long, help = "MQTT broker hostname or IP for Home Assistant (e.g. 192.168.99.4)")]
        mqtt_host: Option<String>,

        #[arg(long, default_value_t = 1883, help = "MQTT broker port")]
        mqtt_port: u16,

        #[arg(long, help = "MQTT broker username")]
        mqtt_user: Option<String>,

        #[arg(long, help = "MQTT broker password")]
        mqtt_pass: Option<String>,
    },

    #[command(about = "Read current thermostat temperature, setpoint, mode, and metadata")]
    Status {
        #[arg(long, help = "Force a fresh BLE read instead of returning cached state")]
        fresh: bool,

        #[arg(long, help = "Output raw JSON")]
        json: bool,
    },

    #[command(about = "Set thermostat target temperature, mode, and/or zone name")]
    Set {
        #[arg(long, help = "Target temperature (33..99°F or 1..37°C)")]
        temp: Option<u8>,

        #[arg(long, help = "Operating mode (e.g. 'COOL AUTO LOW', 'OFF')")]
        mode: Option<String>,

        #[arg(long, help = "Zone name (up to 7 characters)")]
        zone: Option<String>,

        #[arg(long, help = "Output raw JSON")]
        json: bool,
    },

    #[command(about = "Scan for nearby Coleman-Mach Bluetooth thermostats")]
    Scan,

    #[command(about = "Pair with thermostat and enroll in BLE Link-Layer Whitelist")]
    Pair {
        #[arg(long, help = "6-digit thermostat PIN / Unit ID flashing on thermostat LCD (e.g. 123456)")]
        pin: Option<String>,
    },

    #[command(about = "Continuously poll and display thermostat status in real time")]
    Monitor {
        #[arg(long, default_value_t = 5, help = "Polling interval in seconds")]
        interval: u64,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::registry()
        .with(tracing_subscriber::fmt::layer())
        .with(tracing_subscriber::EnvFilter::from_default_env().add_directive(tracing::Level::INFO.into()))
        .init();

    let cli = Cli::parse();
    let manager = Arc::new(ThermostatManager::new(cli.mac));

    match cli.command {
        Commands::Serve {
            host,
            port,
            mqtt_host,
            mqtt_port,
            mqtt_user,
            mqtt_pass,
        } => {
            println!("======================================================================");
            println!(" BreezyMach Web Service (Rust Native Single Binary)");
            println!(" Listening on: http://{}:{}", host, port);
            let mac_display = manager.get_target_mac().await.unwrap_or_else(|_| "Auto-discover".to_string());
            println!(" Target MAC : {}", mac_display);
            if let Some(ref m_host) = mqtt_host {
                println!(" MQTT Broker: {}:{}", m_host, mqtt_port);
            }
            println!("======================================================================");

            if let Some(m_host) = mqtt_host {
                match mqtt::MqttService::start(
                    m_host,
                    mqtt_port,
                    mqtt_user,
                    mqtt_pass,
                    manager.clone(),
                )
                .await
                {
                    Ok(_) => println!("[MQTT] Service initialized and publishing HA auto-discovery."),
                    Err(e) => eprintln!("[MQTT ERROR] Failed to start MQTT service: {}", e),
                }
            }

            let app = server::create_router(manager.clone());
            let addr = format!("{}:{}", host, port);
            let listener = tokio::net::TcpListener::bind(&addr).await?;
            axum::serve(listener, app).await?;
        }

        Commands::Status { fresh, json } => {
            match manager.get_state(fresh).await {
                Ok(state) => {
                    if json {
                        println!("{}", serde_json::to_string_pretty(&state)?);
                    } else {
                        print_human_state(&state);
                    }
                }
                Err(e) => {
                    eprintln!("[ERROR] Failed to read thermostat state: {}", e);
                    std::process::exit(1);
                }
            }
        }

        Commands::Set { temp, mode, zone, json } => {
            if temp.is_none() && mode.is_none() && zone.is_none() {
                eprintln!("Error: Specify at least one of --temp, --mode, or --zone.");
                std::process::exit(2);
            }

            match manager.apply_settings(temp, mode.as_deref(), zone.as_deref()).await {
                Ok(state) => {
                    if json {
                        println!("{}", serde_json::to_string_pretty(&state)?);
                    } else {
                        println!("[SUCCESS] Settings applied to thermostat:");
                        print_human_state(&state);
                    }
                }
                Err(e) => {
                    eprintln!("[ERROR] Failed to apply settings: {}", e);
                    std::process::exit(1);
                }
            }
        }

        Commands::Scan => {
            println!("Scanning for Bluetooth devices via BlueZ...");
            match manager.scan_devices().await {
                Ok(devs) => {
                    for d in devs {
                        println!("Device: {}", d);
                    }
                }
                Err(e) => eprintln!("[ERROR] Scan failed: {}", e),
            }
        }

        Commands::Pair { pin } => {
            println!("======================================================================");
            println!(" Coleman-Mach Thermostat Pairing / Whitelist Enrollment");
            println!("======================================================================");
            println!(" 1. Press and hold BOTH the UP and DOWN arrow buttons on the");
            println!("    thermostat for 5 seconds.");
            println!(" 2. Watching BLE advertisements for Mfg 0xFFFF = 0xAA...");
            let pin_display = pin.as_deref().unwrap_or("<flashing 6-digit PIN>");
            println!(" 3. Auto-Passkey Agent active (PIN: {}).", pin_display);
            println!("======================================================================");

            match manager.run_pairing_flow(pin, 60).await {
                Ok(state) => {
                    println!("\n[SUCCESS] Thermostat paired and enrolled in whitelist!");
                    println!("  Address : {}", state.mac_address);
                    println!("  Zone    : {}", state.zone_name);
                    println!("  Unit ID : {}", state.unit_id);
                    println!("  Setpoint: {}{}", state.set_point.unwrap_or(0), state.temp_unit());
                    println!("  Mode    : {}", state.current_mode);
                }
                Err(e) => {
                    eprintln!("\n[ERROR] Pairing failed: {}", e);
                    std::process::exit(1);
                }
            }
        }

        Commands::Monitor { interval } => {
            println!("Monitoring Coleman-Mach thermostat every {}s (Ctrl+C to stop)...", interval);
            loop {
                match manager.get_state(true).await {
                    Ok(s) => {
                        let t_str = s.temperature.map(|t| format!("{}{}", t, s.temp_unit())).unwrap_or_else(|| "--".to_string());
                        let sp_str = s.set_point.map(|sp| format!("{}{}", sp, s.temp_unit())).unwrap_or_else(|| "--".to_string());
                        println!(
                            "[{}] Zone='{}' | Room={} | SetPoint={} | Mode='{}' | ID={}",
                            s.timestamp, s.zone_name, t_str, sp_str, s.current_mode, s.unit_id
                        );
                    }
                    Err(e) => eprintln!("[WARN] Poll error: {}", e),
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(interval)).await;
            }
        }
    }

    Ok(())
}

fn print_human_state(state: &ThermostatState) {
    let t_str = state.temperature.map(|t| format!("{}{}", t, state.temp_unit())).unwrap_or_else(|| "--".to_string());
    let sp_str = state.set_point.map(|sp| format!("{}{}", sp, state.temp_unit())).unwrap_or_else(|| "--".to_string());
    let avail_str = if state.available_modes.is_empty() { "(none)".to_string() } else { state.available_modes.join(", ") };

    println!("============================================================");
    println!(" Coleman-Mach Thermostat [{}]", state.mac_address);
    println!("============================================================");
    println!("  Zone Name       : {}", state.zone_name);
    println!("  Thermostat ID   : {}", state.unit_id);
    println!("  Room Temp       : {}", t_str);
    println!("  Target Setpoint : {}", sp_str);
    println!("  Current Mode    : {}", state.current_mode);
    println!("  Temp Scale      : {}", if state.is_celsius { "Celsius (°C)" } else { "Fahrenheit (°F)" });
    println!("  Available Modes : {}", avail_str);
    println!("  Avail Raw Hex   : {}", state.available_modes_raw_hex);
    println!("============================================================");
}
