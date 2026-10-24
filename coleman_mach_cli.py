#!/usr/bin/env python3
"""
Coleman-Mach / Airxcel RV Climate Bluetooth Thermostat Controller & Monitor
===========================================================================
Reverse-engineered from the official Android APK:
  Package: com.icmcontrols.rvp.climate (v1.6.15, vercode 15)
  APK SHA-256: 758c008c6e9dd59724e6930c67fdb019f0e97aeca26db4520ed1e3939374117c
  classes.dex SHA-256: 34fef7e9a5a8a98d2937b296fe9a793a514d403cd94407a1a51f7a8b85c2fcaf

 classes:
  - monitor.ble.com.climate.services.BLEGattAttributes
  - monitor.ble.com.climate.services.BLEManager
  - monitor.ble.com.climate.models.BLEObject
  - monitor.ble.com.climate.adapters.SwipeAdapter
  - monitor.ble.com.climate.activities.MainActivity
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# =============================================================================
# BLE GATT Protocol Constants (from BLEGattAttributes.java & BLEManager.java)
# =============================================================================

ADVERTISING_PACKETS_HEX = "02010611071F0681FA66C004A91B498046232728C904FFFFFFAA"
MANUFACTURER_COMPANY_ID = 0xFFFF
MANUFACTURER_PAYLOAD = b"\xaa"

THERMOSTAT_SERVICE_UUID = "c9282723-4680-491b-a904-c066fa81061f"

# Characteristics under THERMOSTAT_SERVICE_UUID
ROOM_TEMPERATURE_CHAR_UUID = "382b4008-084a-4158-b378-66674091b1e2"  # Read (1 byte: uint8 temp; 0xFF = invalid/"--")
ZONE_ID_CHAR_UUID = "beb89473-05fe-41a0-9896-2c082660f19a"           # Read/Write (7-byte ASCII string, space-padded)
SET_POINT_CHAR_UUID = "09996813-fe7d-48ce-9b47-11634c80263c"         # Read/Write (1 byte: uint8 target temp)
AVAILABLE_MODE_CHAR_UUID = "0575cdb7-d448-4bff-bc53-3b900d2a829f"    # Read (bitmask/byte-array for supported modes)
CELSIUS_CHAR_UUID = "3016e3fc-1dbb-455e-b268-80750e36c950"           # Read (1 byte: 1 = Celsius, 0 = Fahrenheit)
MODE_OPERATION_CHAR_UUID = "9230a9ef-347c-4645-8fd9-cbb830d714bf"    # Read/Write (up to 14-byte ASCII mode string)
UNIT_ID_CHAR_UUID = "e21b2b2c-379e-4166-b835-c71ef7eadfdf"           # Read (3 bytes -> 6-digit zero-padded decimal ID)
VALID_RANGE_DESCRIPTOR_UUID = "00002906-0000-1000-8000-00805f9b34fb"

# Exact mode table from BLEManager.<clinit>() (indices 0..9)
MODES: List[str] = [
    "OFF",             # bit 9 (MSB of 10-bit mask, raw[0] in 10-byte array)
    "FAN LOW",         # bit 8
    "FAN HIGH",        # bit 7
    "COOL LOW",        # bit 6
    "COOL HIGH",       # bit 5
    "COOL AUTO LOW",   # bit 4
    "COOL AUTO HIGH",  # bit 3
    "HEAT",            # bit 2
    "HEAT ELEC",       # bit 1
    "HEAT GAS",        # bit 0 (LSB of 10-bit mask, raw[9] in 10-byte array)
]

# Convenient CLI aliases mapped to canonical mode strings
MODE_ALIASES = {
    "off": "OFF",
    "fan-low": "FAN LOW",
    "fan_low": "FAN LOW",
    "fanlow": "FAN LOW",
    "fan-high": "FAN HIGH",
    "fan_high": "FAN HIGH",
    "fanhigh": "FAN HIGH",
    "cool-low": "COOL LOW",
    "cool_low": "COOL LOW",
    "coollow": "COOL LOW",
    "cool-high": "COOL HIGH",
    "cool_high": "COOL HIGH",
    "coolhigh": "COOL HIGH",
    "cool-auto-low": "COOL AUTO LOW",
    "cool_auto_low": "COOL AUTO LOW",
    "coolautolow": "COOL AUTO LOW",
    "cool-auto-high": "COOL AUTO HIGH",
    "cool_auto_high": "COOL AUTO HIGH",
    "coolautohigh": "COOL AUTO HIGH",
    "heat": "HEAT",
    "heat-elec": "HEAT ELEC",
    "heat_elec": "HEAT ELEC",
    "heatelec": "HEAT ELEC",
    "heat-gas": "HEAT GAS",
    "heat_gas": "HEAT GAS",
    "heatgas": "HEAT GAS",
}

# Setpoint temperature bounds from SwipeAdapter.showSettingDlg()
FAHRENHEIT_MIN = 33
FAHRENHEIT_MAX = 99  # start=33, count=67 -> 33..99
CELSIUS_MIN = 1
CELSIUS_MAX = 37     # start=1, count=37 -> 1..37

CONFIG_PATH = Path.home() / ".config" / "coleman_mach_ble.json"


# =============================================================================
# Byte-Level Codecs (Exact match to BLEManager.java & SwipeAdapter.java)
# =============================================================================

def decode_temperature(raw: bytes) -> Optional[int]:
    """
    BLEManager$4.onCharacteristicRead (FEATURE_TEMPERATURE = 1):
    1 byte. 0xFF (255) is treated by SwipeAdapter as '--' (invalid/uninitialized).
    """
    if not raw:
        return None
    val = raw[0] & 0xFF
    if val == 0xFF:
        return None
    # Signed byte in Java (`aget-byte`), though valid room temps are positive
    return struct.unpack("<b", bytes([val]))[0] if val >= 0x80 else val


import struct


def decode_set_point(raw: bytes) -> Optional[int]:
    """
    BLEManager$4.onCharacteristicRead (FEATURE_SET_POINT = 3):
    1 byte (`aget-byte v4, v4, 0`).
    """
    if not raw:
        return None
    return raw[0] & 0xFF


def encode_set_point(set_point: int, is_celsius: bool = False, strict: bool = True) -> bytes:
    """
    BLEManager.writeFeature(String, int):
    1 byte: `(byte)(set_point & 0xFF)`.
    SwipeAdapter.showSettingDlg enforces 1..37 C or 33..99 F.
    """
    if strict:
        lo, hi = (CELSIUS_MIN, CELSIUS_MAX) if is_celsius else (FAHRENHEIT_MIN, FAHRENHEIT_MAX)
        unit = "C" if is_celsius else "F"
        if not (lo <= set_point <= hi):
            raise ValueError(f"Set point {set_point}°{unit} out of valid range ({lo}..{hi}°{unit})")
    return bytes([set_point & 0xFF])


def decode_celsius(raw: bytes) -> bool:
    """
    BLEManager$4.onCharacteristicRead (FEATURE_CELCIUS = 5):
    `raw[0] == 1` -> True (Celsius), else False (Fahrenheit).
    """
    if not raw:
        return False
    return (raw[0] & 0xFF) == 1


def decode_zone_name(raw: bytes) -> str:
    """
    BLEManager.byte2String(raw, 7) + String.trim():
    Reads up to 7 ASCII characters and strips padding spaces/nulls.
    """
    if not raw:
        return ""
    chars = [chr(b & 0xFF) for b in raw[:7] if (b & 0xFF) != 0]
    return "".join(chars).strip()


def encode_zone_name(name: str) -> bytes:
    """
    BLEManager.doWriteAction (FEATURE_WRITE_NAME = 2011):
    `String.format("%1$-7s", wZoneName).getBytes()`
    Left-aligned, space-padded to 7 ASCII bytes (truncated to 7 chars if longer).
    """
    clean = name.strip()[:7]
    return f"{clean:<7s}".encode("ascii")


def decode_current_mode(raw: bytes) -> str:
    """
    BLEManager.byte2String(raw, 14) + String.trim():
    Reads up to 14 ASCII characters and strips whitespace/nulls.
    """
    if not raw:
        return ""
    chars = [chr(b & 0xFF) for b in raw[:14] if (b & 0xFF) != 0]
    return "".join(chars).strip()


def normalize_mode(mode_input: str) -> str:
    """Normalize user mode string (e.g., 'cool-low' or 'COOL LOW') to canonical mode."""
    cleaned = mode_input.strip()
    upper = " ".join(cleaned.upper().split())
    if upper in MODES:
        return upper
    alias = MODE_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    raise ValueError(f"Invalid mode '{mode_input}'. Valid modes: {', '.join(MODES)}")


def encode_current_mode(mode_input: str) -> bytes:
    """
    BLEManager.doWriteAction (FEATURE_WRITE_MODE = 2012):
    `wCurMode.getBytes()` -> raw ASCII bytes of canonical mode string.
    """
    canonical = normalize_mode(mode_input)
    return canonical.encode("ascii")


def bin2hex_java(raw: bytes) -> int:
    """
    Exact replica of BLEManager.bin2Hex(byte[]):
      int res = 0;
      for (int i = 0; i < raw.length; i++) {
          res = (res << 1) | raw[i];
      }
      return res;
    Note: Java's `aget-byte` sign-extends bytes >= 0x80, and `or-int/2addr` ORs the signed int.
    """
    res = 0
    for b in raw:
        signed_b = struct.unpack("<b", bytes([b]))[0]
        res = ((res << 1) | signed_b) & 0xFFFFFFFF
    # Convert back to signed 32-bit int as in Java
    if res & 0x80000000:
        res -= 0x100000000
    return res


def get_modes_list_java(mask: int) -> List[str]:
    """
    Exact replica of BLEManager.getModesList(int):
      ArrayList<String> list = new ArrayList<>();
      int idx = modes.length - 1; // 9 down to 0
      while (mask > 0 && idx >= 0) {
          int bit = mask & 1;
          mask = mask >> 1;
          if (bit == 1) {
              list.add(modes[idx]);
          }
          idx--;
      }
      return list;
    """
    out: List[str] = []
    idx = len(MODES) - 1
    while mask > 0 and idx >= 0:
        bit = mask & 1
        mask = mask >> 1
        if bit == 1:
            out.append(MODES[idx])
        idx -= 1
    return out


def decode_available_modes(raw: bytes) -> List[str]:
    """Decode AVAILABLE_MODE_CHARACTERISTIC via BLEManager.bin2Hex + getModesList."""
    if not raw:
        return []
    mask = bin2hex_java(raw)
    return get_modes_list_java(mask)


def decode_unit_id(raw: bytes) -> str:
    """
    Exact replica of BLEManager.byte2DecString(raw, 3):
    Reads up to 3 bytes; each byte < 10 is prefixed with '0'.
    """
    if not raw:
        return ""
    parts = []
    for b in raw[:3]:
        signed_b = struct.unpack("<b", bytes([b]))[0]
        if signed_b < 10:
            parts.append(f"0{signed_b}")
        else:
            parts.append(str(signed_b))
    return "".join(parts)


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ThermostatState:
    mac_address: str
    ble_name: Optional[str] = None
    zone_name: str = ""
    unit_id: str = ""
    is_celsius: bool = False
    temperature: Optional[int] = None
    set_point: Optional[int] = None
    current_mode: str = ""
    available_modes: List[str] = field(default_factory=list)
    available_modes_raw_hex: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def temp_unit(self) -> str:
        return "°C" if self.is_celsius else "°F"


# =============================================================================
# Bluetooth Adapter Helper & Scanner
# =============================================================================

def get_thermostat_adv_mode(adv: AdvertisementData) -> Optional[str]:
    """
    Returns:
      - 'PAIRING_OPEN (0xAA)' when Mfg 0xFFFF == b'\\xaa' (UP+DOWN held for 5s, whitelist open)
      - 'WHITELIST_ONLY (0x00)' when Mfg 0xFFFF == b'\\x00' (normal operation, only whitelisted MACs accepted)
      - 'UNKNOWN' if service UUID matches but Mfg data differs
      - None if not a Coleman-Mach thermostat
    """
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    mfg = adv.manufacturer_data or {}
    is_service = THERMOSTAT_SERVICE_UUID in uuids
    has_mfg = MANUFACTURER_COMPANY_ID in mfg

    if not is_service and not has_mfg:
        return None
    if has_mfg:
        payload = mfg[MANUFACTURER_COMPANY_ID]
        if payload.startswith(b"\xaa"):
            return "PAIRING_OPEN (0xAA)"
        elif payload.startswith(b"\x00"):
            return "WHITELIST_ONLY (0x00)"
        elif is_service:
            return f"CUSTOM (0x{payload.hex().upper()})"
        return None
    return "THERMOSTAT_UUID_ONLY"


def is_coleman_thermostat(device: BLEDevice, adv: AdvertisementData) -> bool:
    """
    Match a BLE device against BLEGattAttributes.THERMOSTAT_SERVICE or
    manufacturer payload 0xFFFF -> 0xAA / 0x00.
    """
    return get_thermostat_adv_mode(adv) is not None


def ensure_bluetooth_powered() -> None:
    """Ensure hci0 is unblocked by rfkill and powered on via bluetoothctl."""
    try:
        subprocess.run(["rfkill", "unblock", "bluetooth"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["bluetoothctl", "power", "on"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_saved_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def load_saved_mac() -> Optional[str]:
    return load_saved_config().get("mac_address")


def load_saved_pin() -> str:
    return load_saved_config().get("unit_id") or ""


def save_mac(mac_address: str, zone_name: str = "", unit_id: str = "") -> None:
    existing = load_saved_config()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mac_address": mac_address,
        "zone_name": zone_name or existing.get("zone_name", ""),
        "unit_id": unit_id or existing.get("unit_id", ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n")


async def register_bluez_pin_agent(pin_str: Optional[str] = None):
    """
    Registers a BlueZ org.bluez.Agent1 on the system D-Bus to automatically answer
    BLE SMP RequestPasskey / RequestPinCode with the 6-digit thermostat Unit ID / PIN
    (e.g. flashing as 6 digits on the thermostat display).
    """
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method
    from dbus_fast import BusType

    clean_pin = (pin_str or load_saved_pin()).replace(" ", "").strip()
    passkey_int = int(clean_pin) if clean_pin.isdigit() else 0

    class AutoPinAgent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):
            pass

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            return clean_pin

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            return passkey_int

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            pass

        @method()
        def RequestAuthorization(self, device: "o"):
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            pass

        @method()
        def Cancel(self):
            pass

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        agent = AutoPinAgent()
        agent_path = "/com/coleman/thermostat/agent"
        bus.export(agent_path, agent)
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        mgr = proxy.get_interface("org.bluez.AgentManager1")
        await mgr.call_register_agent(agent_path, "KeyboardDisplay")
        await mgr.call_request_default_agent(agent_path)
        return bus
    except Exception:
        return None


async def scan_thermostats(timeout: float = 10.0, show_all: bool = False) -> List[tuple[BLEDevice, AdvertisementData]]:
    ensure_bluetooth_powered()
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches: List[tuple[BLEDevice, AdvertisementData]] = []
    for addr, (dev, adv) in discovered.items():
        if show_all or is_coleman_thermostat(dev, adv):
            matches.append((dev, adv))
    return matches


# =============================================================================
# GATT Read / Write Workflows (Matching BLEManager.java)
# =============================================================================

async def read_thermostat_state(client: BleakClient, mac_address: str, ble_name: Optional[str] = None) -> ThermostatState:
    """
    Reads all 7 characteristics in the exact order of BLEManager.initReadList():
      1. ZONE_ID (2)
      2. CELSIUS (5)
      3. SET_POINT (3)
      4. ROOM_TEMPERATURE (1)
      5. MODE_OPERATION (6)
      6. AVAILABLE_MODE (4)
      7. UNIT_ID (7)
    """
    state = ThermostatState(mac_address=mac_address, ble_name=ble_name)

    # 1. Zone ID
    raw_zone = await client.read_gatt_char(ZONE_ID_CHAR_UUID)
    state.zone_name = decode_zone_name(bytes(raw_zone))

    # 2. Celsius flag
    raw_celsius = await client.read_gatt_char(CELSIUS_CHAR_UUID)
    state.is_celsius = decode_celsius(bytes(raw_celsius))

    # 3. Set Point
    raw_sp = await client.read_gatt_char(SET_POINT_CHAR_UUID)
    state.set_point = decode_set_point(bytes(raw_sp))

    # 4. Room Temperature
    raw_temp = await client.read_gatt_char(ROOM_TEMPERATURE_CHAR_UUID)
    state.temperature = decode_temperature(bytes(raw_temp))

    # 5. Current Mode
    raw_mode = await client.read_gatt_char(MODE_OPERATION_CHAR_UUID)
    state.current_mode = decode_current_mode(bytes(raw_mode))

    # 6. Available Modes
    raw_avail = await client.read_gatt_char(AVAILABLE_MODE_CHAR_UUID)
    state.available_modes_raw_hex = bytes(raw_avail).hex().upper()
    state.available_modes = decode_available_modes(bytes(raw_avail))

    # 7. Unit ID
    raw_unit = await client.read_gatt_char(UNIT_ID_CHAR_UUID)
    state.unit_id = decode_unit_id(bytes(raw_unit))

    state.timestamp = time.time()
    return state


async def write_thermostat_settings(
    client: BleakClient,
    zone_name: Optional[str] = None,
    mode: Optional[str] = None,
    set_point: Optional[int] = None,
    is_celsius: Optional[bool] = None,
) -> None:
    """
    Writes settings in the exact order of BLEManager.doWriteAction():
      1. ZONE_ID_CHARACTERISTIC (if zone_name specified)
      2. MODE_OPERATION_CHARACTERISTIC (if mode specified)
      3. SET_POINT_CHARACTERISTIC (if set_point specified)
    """
    if zone_name is not None:
        payload = encode_zone_name(zone_name)
        await client.write_gatt_char(ZONE_ID_CHAR_UUID, payload, response=True)
        await asyncio.sleep(0.2)

    if mode is not None:
        payload = encode_current_mode(mode)
        await client.write_gatt_char(MODE_OPERATION_CHAR_UUID, payload, response=True)
        await asyncio.sleep(0.2)

    if set_point is not None:
        if is_celsius is None:
            raw_celsius = await client.read_gatt_char(CELSIUS_CHAR_UUID)
            is_celsius = decode_celsius(bytes(raw_celsius))
        payload = encode_set_point(set_point, is_celsius=is_celsius)
        await client.write_gatt_char(SET_POINT_CHAR_UUID, payload, response=True)
        await asyncio.sleep(0.2)


# =============================================================================
# CLI Commands
# =============================================================================

def format_state_human(state: ThermostatState) -> str:
    temp_str = f"{state.temperature}{state.temp_unit}" if state.temperature is not None else "--"
    sp_str = f"{state.set_point}{state.temp_unit}" if state.set_point is not None else "--"
    avail_str = ", ".join(state.available_modes) if state.available_modes else "(none)"
    lines = [
        "============================================================",
        f" Coleman-Mach Thermostat [{state.mac_address}]",
        "============================================================",
        f"  Zone Name       : {state.zone_name or '(unnamed)'}",
        f"  Thermostat ID   : {state.unit_id or '(unknown)'}",
        f"  Room Temp       : {temp_str}",
        f"  Target Setpoint : {sp_str}",
        f"  Current Mode    : {state.current_mode or '(unknown)'}",
        f"  Temp Scale      : {'Celsius (°C)' if state.is_celsius else 'Fahrenheit (°F)'}",
        f"  Available Modes : {avail_str}",
        f"  Avail Raw Hex   : {state.available_modes_raw_hex}",
        "============================================================",
    ]
    return "\n".join(lines)


async def resolve_target_device(explicit_mac: Optional[str], scan_timeout: float = 6.0) -> tuple[BLEDevice, Optional[AdvertisementData]]:
    ensure_bluetooth_powered()
    target_mac = explicit_mac or load_saved_mac()
    discovered = await BleakScanner.discover(timeout=scan_timeout, return_adv=True)

    if target_mac:
        target_upper = target_mac.upper()
        if target_upper in discovered:
            return discovered[target_upper]

    # Otherwise pick the first discovered Coleman-Mach thermostat
    for addr, (dev, adv) in discovered.items():
        if is_coleman_thermostat(dev, adv):
            save_mac(dev.address)
            return dev, adv

    raise RuntimeError(
        "No Coleman-Mach thermostat found during BLE scan. "
        "Ensure the thermostat is powered on and within range."
    )


def explain_connection_error(exc: Exception, mac: str, adv: Optional[AdvertisementData]) -> None:
    adv_mode = get_thermostat_adv_mode(adv) if adv else "UNKNOWN"
    print(
        f"\n[ERROR] BLE GATT connection to {mac} failed ({type(exc).__name__}: {exc}).\n"
        f"  Observed Advertising Mode: {adv_mode}\n"
        f"  Why this happens:\n"
        f"    When the Coleman-Mach thermostat advertises Mfg 0xFFFF = 0x00, its Cypress BLE\n"
        f"    Link-Layer Whitelist only accepts connections from already-enrolled central MACs\n"
        f"    (dropping un-enrolled CONNECT_IND packets with HCI 0x3E / le-connection-abort-by-local),\n"
        f"    OR another phone/tablet is actively holding the BLE connection.\n"
        f"  How to enroll this computer (78:2B:46:D9:78:3E):\n"
        f"    1. Run: {sys.argv[0]} pair\n"
        f"    2. Press and hold BOTH the UP and DOWN arrow buttons on the thermostat for 5 seconds.\n"
        f"    3. The thermostat will switch its advertisement to Mfg 0xFFFF = 0xAA (Open Pairing Mode),\n"
        f"       and this tool will automatically connect and enroll this localhost!\n",
        file=sys.stderr,
    )


async def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning BLE for {args.timeout:.1f}s (filter={'ALL' if args.all else 'Coleman-Mach'})...", file=sys.stderr)
    matches = await scan_thermostats(timeout=args.timeout, show_all=args.all)
    if args.json:
        rows = []
        for dev, adv in matches:
            rows.append({
                "address": dev.address,
                "name": dev.name or adv.local_name,
                "rssi": adv.rssi,
                "is_coleman_thermostat": is_coleman_thermostat(dev, adv),
                "thermostat_adv_mode": get_thermostat_adv_mode(adv),
                "service_uuids": adv.service_uuids,
                "manufacturer_data": {hex(k): v.hex().upper() for k, v in (adv.manufacturer_data or {}).items()},
            })
        print(json.dumps(rows, indent=2))
        return 0

    if not matches:
        print("No matching BLE devices found.")
        return 1

    for dev, adv in matches:
        adv_mode = get_thermostat_adv_mode(adv)
        tag = f"[COLEMAN-MACH {adv_mode}]" if adv_mode else "[OTHER BLE]"
        mfg_hex = {f"0x{k:04X}": v.hex().upper() for k, v in (adv.manufacturer_data or {}).items()}
        print(f"{tag} MAC: {dev.address} | Name: {dev.name or adv.local_name or '(none)'} | RSSI: {adv.rssi} dBm | Services: {adv.service_uuids} | Mfg: {mfg_hex}")
        if adv_mode:
            save_mac(dev.address)
            print(f"  -> Saved default thermostat MAC {dev.address} to {CONFIG_PATH}")
    return 0


async def cmd_pair(args: argparse.Namespace) -> int:
    ensure_bluetooth_powered()
    _bus = await register_bluez_pin_agent(args.pin)
    print("======================================================================")
    print(" Coleman-Mach Thermostat Pairing / Whitelist Enrollment")
    print("======================================================================")
    print(" 1. Press and hold BOTH the UP and DOWN arrow buttons on the")
    print("    thermostat for 5 seconds.")
    print(" 2. Watching BLE advertisements for Mfg 0xFFFF = 0xAA...")
    print(f" 3. Auto-Passkey Agent active (PIN: {args.pin or load_saved_pin()}).")
    print("======================================================================")

    found_event = asyncio.Event()
    target_holder: List[tuple[BLEDevice, AdvertisementData]] = []
    last_status_print = 0.0

    def on_detect(dev: BLEDevice, adv: AdvertisementData) -> None:
        nonlocal last_status_print
        mode = get_thermostat_adv_mode(adv)
        if not mode:
            return
        if args.mac and dev.address.upper() != args.mac.upper():
            return
        now = time.time()
        if mode == "PAIRING_OPEN (0xAA)":
            print(f"\n[!] Detected thermostat {dev.address} in OPEN PAIRING MODE (0xAA) at RSSI {adv.rssi} dBm!")
            target_holder.append((dev, adv))
            found_event.set()
        elif now - last_status_print >= 3.0:
            last_status_print = now
            print(f"  [.] Thermostat {dev.address} seen (RSSI {adv.rssi} dBm, mode={mode}) — waiting for 5-sec UP+DOWN button press (0xAA)...")

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        await asyncio.wait_for(found_event.wait(), timeout=args.wait)
    except asyncio.TimeoutError:
        await scanner.stop()
        print(f"\n[TIMEOUT] Did not see Mfg 0xFFFF = 0xAA within {args.wait:.0f}s.", file=sys.stderr)
        return 1
    await scanner.stop()
    await asyncio.sleep(0.2)

    dev, adv = target_holder[0]
    print(f"Connecting and bonding to {dev.address} to complete whitelist enrollment...")
    last_exc: Optional[Exception] = None
    for attempt in range(1, 5):
        try:
            fresh_dev = await BleakScanner.find_device_by_address(dev.address, timeout=6.0)
            async with BleakClient(fresh_dev or dev, timeout=args.timeout) as client:
                try:
                    await client.pair()
                except Exception:
                    pass
                state = await read_thermostat_state(client, mac_address=dev.address)
                save_mac(dev.address, zone_name=state.zone_name, unit_id=state.unit_id)
                print("\n[SUCCESS] Enrolled and read thermostat state:")
                print(format_state_human(state))
                return 0
        except Exception as exc:
            last_exc = exc
            print(f"  [Attempt {attempt}/4] Connection attempt returned: {exc}; retrying in 0.6s...")
            await asyncio.sleep(0.6)

    if last_exc is not None:
        explain_connection_error(last_exc, dev.address, adv)
    return 1


async def cmd_status(args: argparse.Namespace) -> int:
    _bus = await register_bluez_pin_agent(args.pin)
    dev, adv = await resolve_target_device(args.mac, scan_timeout=min(args.timeout, 6.0))
    try:
        async with BleakClient(dev, timeout=args.timeout) as client:
            state = await read_thermostat_state(client, mac_address=dev.address)
            save_mac(dev.address, zone_name=state.zone_name, unit_id=state.unit_id)
            if args.json:
                print(json.dumps(asdict(state), indent=2))
            else:
                print(format_state_human(state))
        return 0
    except Exception as exc:
        explain_connection_error(exc, dev.address, adv)
        return 1


async def cmd_set(args: argparse.Namespace) -> int:
    if args.temp is None and args.mode is None and args.zone is None:
        print("Error: Specify at least one of --temp, --mode, or --zone.", file=sys.stderr)
        return 2

    _bus = await register_bluez_pin_agent(args.pin)
    dev, adv = await resolve_target_device(args.mac, scan_timeout=min(args.timeout, 6.0))
    try:
        async with BleakClient(dev, timeout=args.timeout) as client:
            await write_thermostat_settings(
                client,
                zone_name=args.zone,
                mode=args.mode,
                set_point=args.temp,
            )
            # Wait 500ms as BLEManager.java does before reading back state
            await asyncio.sleep(0.5)
            state = await read_thermostat_state(client, mac_address=dev.address)
            save_mac(dev.address, zone_name=state.zone_name, unit_id=state.unit_id)
            if args.json:
                print(json.dumps(asdict(state), indent=2))
            else:
                print(format_state_human(state))
        return 0
    except Exception as exc:
        explain_connection_error(exc, dev.address, adv)
        return 1


async def cmd_monitor(args: argparse.Namespace) -> int:
    _bus = await register_bluez_pin_agent(args.pin)
    dev, adv = await resolve_target_device(args.mac, scan_timeout=min(args.timeout, 6.0))
    mac = dev.address
    print(f"Monitoring Coleman-Mach thermostat {mac} every {args.interval:.1f}s (Ctrl+C to stop)...", file=sys.stderr)
    while True:
        try:
            async with BleakClient(dev, timeout=args.timeout) as client:
                while client.is_connected:
                    state = await read_thermostat_state(client, mac_address=mac)
                    if args.json:
                        print(json.dumps(asdict(state)), flush=True)
                    else:
                        ts = time.strftime("%H:%M:%S")
                        t_str = f"{state.temperature}{state.temp_unit}" if state.temperature is not None else "--"
                        sp_str = f"{state.set_point}{state.temp_unit}" if state.set_point is not None else "--"
                        print(
                            f"[{ts}] Zone='{state.zone_name}' | Room={t_str} | SetPoint={sp_str} | Mode='{state.current_mode}' | ID={state.unit_id}",
                            flush=True,
                        )
                    await asyncio.sleep(args.interval)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[WARN] Connection/read error ({exc}); retrying in {args.interval:.1f}s...", file=sys.stderr)
            await asyncio.sleep(args.interval)
    return 0


def run_selftest() -> int:
    print("Running Coleman-Mach BLE protocol codec self-tests...")

    # 1. Verify advertisement packet contains little-endian THERMOSTAT_SERVICE_UUID
    adv_bytes = bytes.fromhex(ADVERTISING_PACKETS_HEX)
    assert adv_bytes[:3] == b"\x02\x01\x06", "Flags AD structure mismatch"
    assert adv_bytes[3:5] == b"\x11\x07", "128-bit UUID AD header mismatch"
    uuid_le = adv_bytes[5:21]
    # Reconstruct standard Bluetooth 128-bit UUID from reversed 16-byte BLE AD field
    import uuid
    parsed_uuid = str(uuid.UUID(bytes=uuid_le[::-1]))
    assert parsed_uuid == THERMOSTAT_SERVICE_UUID, f"Expected {THERMOSTAT_SERVICE_UUID}, got {parsed_uuid}"
    assert adv_bytes[21:] == b"\x04\xff\xff\xff\xaa", "Manufacturer AD structure mismatch"
    print(f"  [PASS] Advertising packet decodes to Service UUID {parsed_uuid} + Mfg 0xFFFF:0xAA")

    # 2. Temperature & SetPoint codecs
    assert decode_temperature(b"\xff") is None
    assert decode_temperature(b"\x48") == 72
    assert decode_set_point(b"\x44") == 68
    assert encode_set_point(70, is_celsius=False) == b"\x46"
    assert encode_set_point(22, is_celsius=True) == b"\x16"
    print("  [PASS] Temperature and SetPoint codecs verified (33..99°F, 1..37°C)")

    # 3. Zone Name 7-byte space-padded codec
    encoded_zone = encode_zone_name("FRONT")
    assert encoded_zone == b"FRONT  " and len(encoded_zone) == 7
    assert decode_zone_name(encoded_zone) == "FRONT"
    print("  [PASS] Zone ID 7-byte space-padded ASCII codec verified")

    # 4. Mode codec & Available Modes bit-array decoder
    assert encode_current_mode("cool-auto-low") == b"COOL AUTO LOW"
    assert decode_current_mode(b"COOL AUTO LOW ") == "COOL AUTO LOW"
    # Test all 10 modes enabled (10 bytes of 0x01)
    all_modes_raw = bytes([1] * 10)
    decoded_all = decode_available_modes(all_modes_raw)
    assert set(decoded_all) == set(MODES) and len(decoded_all) == 10
    # Test subset: OFF (idx 0 -> raw[0]=1), FAN LOW (idx 1 -> raw[1]=1), COOL LOW (idx 3 -> raw[3]=1), HEAT GAS (idx 9 -> raw[9]=1)
    subset_raw = bytes([1, 1, 0, 1, 0, 0, 0, 0, 0, 1])
    decoded_subset = decode_available_modes(subset_raw)
    assert decoded_subset == ["HEAT GAS", "COOL LOW", "FAN LOW", "OFF"], f"Got {decoded_subset}"
    print(f"  [PASS] Available Modes bit-shift decoder verified: {decoded_subset}")

    # 5. Unit ID 3-byte decimal string decoder
    assert decode_unit_id(bytes([4, 15, 9])) == "041509"
    print("  [PASS] Unit ID 3-byte decimal string decoder verified ('041509')")

    print("All Coleman-Mach BLE protocol self-tests PASSED.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coleman-Mach / Airxcel RV Climate BLE Thermostat Monitor & Controller"
    )
    parser.add_argument("--mac", help="Thermostat Bluetooth MAC address (default: auto-discover or saved)")
    parser.add_argument("--pin", help="6-digit Bluetooth SMP passkey / Unit ID flashing on thermostat display")
    parser.add_argument("--timeout", type=float, default=10.0, help="BLE scan/connect timeout in seconds (default: 10)")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan for Coleman-Mach Bluetooth thermostats")
    p_scan.add_argument("--all", action="store_true", help="Show all nearby BLE devices, not just thermostats")

    p_pair = sub.add_parser(
        "pair",
        help="Wait for thermostat 0xAA pairing advertisement (hold UP+DOWN 5s) and enroll localhost in BLE whitelist",
    )
    p_pair.add_argument("--wait", type=float, default=60.0, help="Seconds to wait for 0xAA pairing mode (default: 60)")

    sub.add_parser("status", help="Read current thermostat temperature, setpoint, mode, and metadata")

    p_set = sub.add_parser("set", help="Set thermostat target temperature, mode, and/or zone name")
    p_set.add_argument("--temp", type=int, help="Target setpoint temperature (33..99°F or 1..37°C)")
    p_set.add_argument(
        "--mode",
        help=f"Operating mode ({', '.join(MODES)})",
    )
    p_set.add_argument("--zone", help="Zone name (up to 7 ASCII characters, e.g. FRONT, CENTER, BACK)")

    p_mon = sub.add_parser("monitor", help="Continuously poll and display thermostat status")
    p_mon.add_argument("--interval", type=float, default=60.0, help="Polling interval in seconds (default: 60.0)")

    sub.add_parser("selftest", help="Run protocol codec unit tests against reverse-engineered APK semantics")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "selftest":
        return run_selftest()
    elif args.command == "scan":
        return asyncio.run(cmd_scan(args))
    elif args.command == "pair":
        return asyncio.run(cmd_pair(args))
    elif args.command == "status":
        return asyncio.run(cmd_status(args))
    elif args.command == "set":
        return asyncio.run(cmd_set(args))
    elif args.command == "monitor":
        try:
            return asyncio.run(cmd_monitor(args))
        except KeyboardInterrupt:
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
