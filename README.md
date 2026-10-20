# BreezyMach 🚐❄️
**Compatible Engineered Bluetooth controller and local web dashboard for Coleman-Mach RV thermostats**

[![Rust 1.80+](https://img.shields.io/badge/rust-1.80+-orange.svg)](https://www.rust-lang.org/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20%28BlueZ%29-orange.svg)](#)
[![BLE GATT](https://img.shields.io/badge/Bluetooth-BLE%20GATT-informational.svg)](#)

BreezyMach is a lightweight, local-only Linux tool and web dashboard for monitoring and controlling **Coleman-Mach / Airxcel Bluetooth RV Thermostats**. It runs 100% offline inside your RV without any cloud accounts, proprietary gateways, or internet access. Available both as a high-performance **native Rust single binary** and as a **Python service**.

<p align="center">
  <img src="dashboard_screenshot.png" alt="BreezyMach Web Dashboard" width="460" style="border-radius: 14px; box-shadow: 0 8px 24px rgba(0,0,0,0.4);" />
</p>

---

## 🌟 Key Features

- **📱 Ultra-Responsive Web Dashboard**: Clean dark-mode dashboard tailored for phones, tablets, and RV touchscreens.
- **⚡ Zero-Lag Instant Controls (0ms Optimistic UI)**: Modes switch immediately on screen and sync to the unit via persistent BLE GATT in ~0.5s.
- **🌡️ Smooth Setpoint Adjustment**: Rapid `+` / `−` temperature adjustments with automatic 400ms debounce.
- **🔗 In-Browser Pairing**: Step-by-step visual instructions to enroll new thermostats and whitelist your Linux computer.
- **💻 Full-Featured CLI**: Control and poll the thermostat via terminal or system scripts (`coleman_mach_cli.py`).
- **🌐 REST API**: JSON endpoints for Home Assistant, Node-RED, or custom automation scripts.
- **🏕️ 100% Offline**: Self-contained CSS and SVG icons—zero external CDNs or internet required.

---

## 📖 Protocol & Architecture

BreezyMach communicates directly with the physical thermostat hardware using standard Bluetooth Low Energy (BLE) GATT characteristics.

### Architecture Highlights:

1. **GATT Services & Characteristics**:
   - Communicates with primary GATT service `c9282723-4680-491b-a904-c066fa81061f` across 7 standard characteristics for room temperature, setpoint, mode, status, and device metadata.
2. **Link-Layer Whitelist Handling**:
   - In normal operation, the thermostat advertises `Mfg 0xFFFF: 0x00` and enforces a hardware whitelist filter policy.
   - Holding **UP + DOWN** on the physical thermostat for 5 seconds switches the advertisement to `0xAA` (`PAIRING_OPEN`), opening the whitelist for enrollment.
3. **Automated BlueZ Passkey Agent**:
   - When entering pairing mode, the thermostat LCD flashes its unique 6-digit Unit ID (flashing 2 digits at a time).
   - BreezyMach registers a BlueZ D-Bus `org.bluez.Agent1` that automatically answers Bluetooth SMP pairing passkey requests, seamlessly completing bonding and whitelist enrollment.
4. **Persistent Low-Latency Connection**:
   - Direct MAC addressing and persistent GATT sessions eliminate BLE discovery scan delays, bringing command round-trip times down to ~0.5 seconds.

---

## 📡 Protocol Specification

### Primary GATT Service: `c9282723-4680-491b-a904-c066fa81061f`

| Feature ID | Characteristic UUID | Access | Wire Format & Semantics |
| :--- | :--- | :---: | :--- |
| `1` | `382b4008-084a-4158-b378-66674091b1e2` | **Read** | **1–2 bytes (`uint8` at `raw[0]`)**: Current ambient room temperature in active scale (°F or °C). `0xFF` (`255`) = uninitialized (`"--"`). |
| `2` / `2011` | `beb89473-05fe-41a0-9896-2c082660f19a` | **Read / Write** | **7-byte ASCII string**: Zone name (e.g. `"Main   "`). Space-padded on write. |
| `3` / `2013` | `09996813-fe7d-48ce-9b47-11634c80263c` | **Read / Write** | **1 byte (`uint8`)**: Target setpoint temperature (33..99°F or 1..37°C). |
| `4` | `0575cdb7-d448-4bff-bc53-3b900d2a829f` | **Read** | **10-byte bit-array**: Enabled operating modes shifted into a 10-bit mask. |
| `5` | `3016e3fc-1dbb-455e-b268-80750e36c950` | **Read** | **1 byte (`uint8`)**: `0x01` = Celsius (`°C`), `0x00` = Fahrenheit (`°F`). |
| `6` / `2012` | `9230a9ef-347c-4645-8fd9-cbb830d714bf` | **Read / Write** | **14–15 byte ASCII string**: Active operating mode (e.g. `"COOL AUTO HIGH"`, `"OFF"`). |
| `7` | `e21b2b2c-379e-4166-b835-c71ef7eadfdf` | **Read** | **3 bytes**: 6-digit decimal unit identifier and BLE SMP pairing passkey. |

---

## 🦀 Rust Native Binary (Zero Dependencies)

BreezyMach includes a high-performance **native Rust** implementation (`breezy-mach`). The complete HTML/CSS/JS web dashboard is compiled directly into a single standalone binary (~7.5 MB) with sub-second startup, low memory footprint (~10-15 MB RAM vs ~60 MB in Python), and direct BlueZ D-Bus integration via Tokio and Bluer.

### Building from Source
```bash
cd rust
cargo build --release
# The compiled standalone binary is in rust/target/release/breezy-mach
```

### Running the Rust Web Server
```bash
./breezy-mach serve --port 8050
```

### Rust CLI Commands
```bash
# Get instant status (human-readable or JSON)
./breezy-mach status
./breezy-mach status --json

# Set temperature and mode
./breezy-mach set --temp 72 --mode "COOL AUTO LOW"
./breezy-mach set --mode OFF

# Scan for Coleman-Mach units
./breezy-mach scan

# Pair a new thermostat with flashing PIN
./breezy-mach pair --pin 123456

# Continuous real-time monitor
./breezy-mach monitor --interval 5
```

---

## 🐍 Python Quickstart

### Prerequisites
- Linux computer with Bluetooth 4.0+ (Raspberry Pi, Linux laptop, RV home server).
- Python 3.9+ with `bleak`, `dbus-fast`, and `aiohttp`.

```bash
git clone https://github.com/chieftechnicalwizard/breezy-mach.git
cd breezy-mach
pip install -r requirements.txt
```

### 1. Launch the Web Dashboard
```bash
./web_app.py --port 8050
```
Open **`http://localhost:8050/`** (or your RV server's local IP) on your phone or computer.

### 2. Pairing a New Unit
If pairing for the first time:
1. Open the dashboard and click **🔗 Pair New Unit**.
2. Stand near your thermostat, press and hold **BOTH the UP and DOWN** buttons for **5 seconds** until the screen flashes its 6-digit PIN.
3. Enter the PIN in the modal and click **Start Pairing**.
4. BreezyMach will pair, save the configuration to `~/.config/coleman_mach_ble.json`, and connect automatically from then on.

---

## 🛠️ CLI Usage (`coleman_mach_cli.py`)

BreezyMach includes a standalone command-line tool for terminal automation and scripts:

```bash
# Scan for thermostats
./coleman_mach_cli.py scan

# Read current status (human readable or JSON)
./coleman_mach_cli.py status
./coleman_mach_cli.py --json status

# Change temperature and mode
./coleman_mach_cli.py set --temp 72 --mode "COOL AUTO LOW"

# Turn thermostat off
./coleman_mach_cli.py set --mode OFF

# Continuous live monitoring (poll every 5 seconds)
./coleman_mach_cli.py monitor --interval 5
```

---

## 🔌 REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/status` | Read current status (`?fresh=true` forces fresh BLE read) |
| `POST` | `/api/set` | Set target setpoint, mode, or zone (e.g. `{"temp": 72, "mode": "COOL AUTO LOW"}`) |
| `GET` | `/api/scan` | Scan for nearby Coleman-Mach BLE units |
| `POST` | `/api/pair` | Initiate background pairing sequence (`{"pin": "123456", "wait": 60}`) |
| `GET` | `/api/pair/status` | Get active pairing progress |
| `GET` | `/api/config` | Read saved configuration |

---

## 📄 License

MIT License. Created by [ChiefTechnicalWizard](https://github.com/chieftechnicalwizard).
Not affiliated with or endorsed by Airxcel, Inc. or ICM Controls Corp.
