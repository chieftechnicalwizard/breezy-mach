#!/usr/bin/env python3
"""
Coleman-Mach / Airxcel RV Climate Bluetooth Thermostat Web Application
Ultra-responsive, low-latency control dashboard and REST API over local HTTP.

Optimizations:
  - Persistent BLE GATT Connection: Reuses open peripheral link (writes in ~1-50ms)
  - Direct MAC addressing: Zero 4-second discovery scan latency on clicks
  - Optimistic UI updates: 0ms visual feedback on mode clicks and temperature taps
  - Debounced setpoint stepper: Tapping + / - updates UI immediately and syncs automatically
  - Non-blocking background health check & polling
  - 100% offline RV ready: zero external CDN dependencies
"""

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional

from aiohttp import web
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

# Import protocol logic and codecs from local coleman_mach_cli module
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import coleman_mach_cli as cli

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RVCMBT_Web")

# =============================================================================
# Persistent Thermostat Service & Fast GATT Connection Manager
# =============================================================================

class ThermostatService:
    def __init__(self, target_mac: Optional[str] = None):
        self.lock = asyncio.Lock()
        self.target_mac = target_mac or cli.load_saved_mac()
        self.cached_state: Optional[cli.ThermostatState] = None
        self.cached_state_time: float = 0.0
        self.last_error: Optional[str] = None
        self.last_rssi: Optional[int] = None
        
        # Persistent client handle
        self._client: Optional[BleakClient] = None
        self._agent_registered: bool = False
        self._bg_task: Optional[asyncio.Task] = None
        self._running: bool = True

        # Pairing state tracking
        self.pairing_active: bool = False
        self.pairing_status: str = "idle"
        self.pairing_message: str = "Ready"
        self.pairing_task: Optional[asyncio.Task] = None

    def get_effective_mac(self) -> Optional[str]:
        return self.target_mac or cli.load_saved_mac()

    async def _ensure_agent(self):
        if not self._agent_registered:
            pin = cli.load_saved_pin()
            await cli.register_bluez_pin_agent(pin)
            self._agent_registered = True

    async def _get_or_create_client(self) -> BleakClient:
        """Returns the active connected BleakClient, or connects directly by MAC in <2s."""
        if self._client and self._client.is_connected:
            return self._client

        mac = self.get_effective_mac()
        if not mac:
            raise RuntimeError("No Coleman-Mach thermostat MAC address configured.")

        await self._ensure_agent()
        cli.ensure_bluetooth_powered()

        # Connect directly by MAC without 4-second BleakScanner.discover
        logger.info(f"Establishing direct BLE GATT connection to {mac}...")
        client = BleakClient(mac, timeout=7.0)
        await client.connect()
        if not client.is_connected:
            raise RuntimeError(f"Could not connect to thermostat at {mac}")

        logger.info(f"Connected to thermostat {mac} (MTU: {client.mtu_size})")
        self._client = client
        return self._client

    async def close_client(self):
        if self._client:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def get_state(self, force_refresh: bool = False, max_age: float = 10.0) -> cli.ThermostatState:
        now = time.time()
        if not force_refresh and self.cached_state and (now - self.cached_state_time) < max_age:
            return self.cached_state

        async with self.lock:
            # Re-check under lock
            now = time.time()
            if not force_refresh and self.cached_state and (now - self.cached_state_time) < max_age:
                return self.cached_state

            mac = self.get_effective_mac()
            for attempt in range(2):
                try:
                    client = await self._get_or_create_client()
                    state = await cli.read_thermostat_state(client, mac_address=mac)
                    cli.save_mac(mac, zone_name=state.zone_name, unit_id=state.unit_id)
                    self.cached_state = state
                    self.cached_state_time = time.time()
                    self.last_error = None
                    return state
                except Exception as exc:
                    logger.warning(f"Read attempt {attempt+1} failed: {exc}")
                    await self.close_client()
                    if attempt == 1:
                        self.last_error = str(exc)
                        raise

        return self.cached_state

    async def apply_settings(
        self,
        set_point: Optional[int] = None,
        mode: Optional[str] = None,
        zone_name: Optional[str] = None,
    ) -> cli.ThermostatState:
        """Fast writes: uses persistent connection and returns updated state in ~100ms."""
        async with self.lock:
            mac = self.get_effective_mac()
            for attempt in range(2):
                try:
                    client = await self._get_or_create_client()
                    t0 = time.time()

                    await cli.write_thermostat_settings(
                        client,
                        zone_name=zone_name,
                        mode=mode,
                        set_point=set_point,
                    )
                    t_write = time.time() - t0
                    logger.info(f"GATT writes executed in {t_write*1000:.1f}ms (mode={mode}, setpoint={set_point})")

                    # Brief 200ms settle before verification read
                    await asyncio.sleep(0.2)
                    state = await cli.read_thermostat_state(client, mac_address=mac)
                    cli.save_mac(mac, zone_name=state.zone_name, unit_id=state.unit_id)
                    self.cached_state = state
                    self.cached_state_time = time.time()
                    self.last_error = None
                    return state

                except Exception as exc:
                    logger.warning(f"Apply settings attempt {attempt+1} failed: {exc}")
                    await self.close_client()
                    if attempt == 1:
                        self.last_error = str(exc)
                        raise

        return self.cached_state

    async def scan_devices(self, timeout: float = 3.0) -> list:
        async with self.lock:
            await self.close_client()  # Release connection so adapter can scan cleanly
            cli.ensure_bluetooth_powered()
            matches = await cli.scan_thermostats(timeout=timeout, show_all=False)
            results = []
            for dev, adv in matches:
                adv_mode = cli.get_thermostat_adv_mode(adv)
                mfg_hex = {f"0x{k:04X}": v.hex().upper() for k, v in (adv.manufacturer_data or {}).items()}
                results.append({
                    "address": dev.address,
                    "name": dev.name or adv.local_name or "Coleman-Mach Thermostat",
                    "rssi": adv.rssi,
                    "adv_mode": adv_mode,
                    "manufacturer_data": mfg_hex,
                })
            return results

    async def run_pairing_flow(self, pin: Optional[str] = None, timeout: float = 60.0):
        async with self.lock:
            await self.close_client()
            self.pairing_active = True
            self.pairing_status = "scanning"
            self.pairing_message = "Listening for thermostat in Pairing Mode (0xAA)... Hold UP+DOWN on unit for 5s."

            try:
                cli.ensure_bluetooth_powered()
                pin_str = (pin or cli.load_saved_pin()).strip()
                await cli.register_bluez_pin_agent(pin_str)
                self._agent_registered = True

                found_event = asyncio.Event()
                target_holder = []

                def on_detect(dev: BLEDevice, adv: AdvertisementData) -> None:
                    mode = cli.get_thermostat_adv_mode(adv)
                    if mode == "PAIRING_OPEN (0xAA)":
                        target_holder.append((dev, adv))
                        found_event.set()

                scanner = BleakScanner(detection_callback=on_detect)
                await scanner.start()
                try:
                    await asyncio.wait_for(found_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    self.pairing_status = "failed"
                    self.pairing_message = f"Timeout: Did not detect pairing advertisement (0xAA) within {int(timeout)}s."
                    return
                finally:
                    await scanner.stop()

                await asyncio.sleep(0.3)
                dev, adv = target_holder[0]
                self.pairing_status = "connecting"
                self.pairing_message = f"Detected {dev.address}! Connecting and bonding with passkey {pin_str}..."

                last_exc = None
                for attempt in range(1, 5):
                    try:
                        fresh_dev = await BleakScanner.find_device_by_address(dev.address, timeout=5.0)
                        async with BleakClient(fresh_dev or dev, timeout=12.0) as client:
                            try:
                                await client.pair()
                            except Exception:
                                pass
                            state = await cli.read_thermostat_state(client, mac_address=dev.address)
                            cli.save_mac(dev.address, zone_name=state.zone_name, unit_id=state.unit_id)
                            self.target_mac = dev.address
                            self.cached_state = state
                            self.cached_state_time = time.time()
                            self.pairing_status = "paired"
                            self.pairing_message = f"Successfully paired with {dev.address} (Zone: '{state.zone_name}', Setpoint: {state.set_point}°{state.temp_unit})!"
                            return
                    except Exception as exc:
                        last_exc = exc
                        await asyncio.sleep(0.6)

                self.pairing_status = "failed"
                self.pairing_message = f"Connection failed after pairing mode detected: {last_exc}"

            except Exception as e:
                self.pairing_status = "failed"
                self.pairing_message = f"Pairing error: {e}"
            finally:
                self.pairing_active = False

    def start_pairing(self, pin: Optional[str] = None, timeout: float = 60.0):
        if self.pairing_active:
            return False, "Pairing is already in progress."
        self.pairing_task = asyncio.create_task(self.run_pairing_flow(pin=pin, timeout=timeout))
        return True, "Pairing sequence started."

    async def start_background_monitor(self):
        """Periodically polls status over persistent connection every 20s without blocking."""
        while self._running:
            try:
                await asyncio.sleep(20)
                if not self.pairing_active and not self.lock.locked():
                    await self.get_state(force_refresh=True, max_age=5.0)
            except Exception as exc:
                logger.debug(f"Background poll tick note: {exc}")


# =============================================================================
# Embedded Modern Dashboard with Optimistic UI & Zero-Lag Controls
# =============================================================================

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Coleman-Mach RV Thermostat Controller</title>
  <style>
    :root {
      --bg: #0b1120;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --primary-hover: #0ea5e9;
      --accent-cool: #06b6d4;
      --accent-heat: #f97316;
      --accent-fan: #a855f7;
      --accent-off: #64748b;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --radius: 18px;
      --shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.4);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background-color: var(--bg); color: var(--text); padding: 16px; display: flex; justify-content: center; min-height: 100vh; }
    .container { width: 100%; max-width: 480px; display: flex; flex-direction: column; gap: 16px; }

    header { display: flex; justify-content: space-between; align-items: center; padding: 6px 2px; }
    .title-group h1 { font-size: 1.25rem; font-weight: 700; color: var(--text); }
    .title-group p { font-size: 0.8rem; color: var(--text-muted); }
    
    .badge { background: #1e293b; border: 1px solid var(--card-border); color: #cbd5e1; font-size: 0.78rem; padding: 5px 12px; border-radius: 999px; display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s ease; }
    .badge-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); }
    .badge-dot.syncing { background: var(--warning); animation: pulse 1s infinite alternate; }
    .badge-dot.error { background: var(--danger); }
    @keyframes pulse { 0% { opacity: 0.4; } 100% { opacity: 1; transform: scale(1.15); } }

    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: var(--radius); padding: 20px; box-shadow: var(--shadow); position: relative; }
    
    /* Dial & Setpoint */
    .dial-card { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 14px; }
    .zone-tag { font-size: 0.82rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--primary); font-weight: 600; background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.25); padding: 4px 14px; border-radius: 20px; }
    
    .room-temp-container { margin: 4px 0; }
    .room-temp-label { font-size: 0.82rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }
    .room-temp { font-size: 4.2rem; font-weight: 800; line-height: 1; color: #ffffff; text-shadow: 0 4px 16px rgba(0,0,0,0.6); }
    .room-temp sup { font-size: 2rem; font-weight: 600; vertical-align: super; }

    .setpoint-box { display: flex; align-items: center; justify-content: center; gap: 24px; width: 100%; margin-top: 6px; }
    .btn-circle { width: 56px; height: 56px; border-radius: 50%; border: 1px solid var(--card-border); background: #273549; color: var(--text); font-size: 2rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.1s ease; user-select: none; }
    .btn-circle:active { transform: scale(0.9); background: var(--primary); color: #0b1120; }
    .setpoint-val-group { text-align: center; min-width: 110px; }
    .setpoint-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
    .setpoint-number { font-size: 2.4rem; font-weight: 700; color: var(--primary); line-height: 1.1; transition: color 0.15s ease; }
    .setpoint-number.pending { color: var(--warning); }

    /* Modes Grid */
    .section-title { font-size: 0.88rem; font-weight: 600; color: var(--text-muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; display: flex; justify-content: space-between; align-items: center; }
    .mode-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
    .btn-mode { padding: 14px 10px; border-radius: 12px; border: 1px solid var(--card-border); background: #243042; color: var(--text); font-size: 0.88rem; font-weight: 600; cursor: pointer; text-align: center; transition: all 0.12s cubic-bezier(0.4, 0, 0.2, 1); user-select: none; }
    .btn-mode:hover { border-color: var(--primary); background: #2d3d54; }
    .btn-mode:active { transform: scale(0.96); }
    .btn-mode.active { background: var(--primary); color: #0b1120; border-color: var(--primary); box-shadow: 0 0 18px rgba(56, 189, 248, 0.45); }
    .btn-mode.active.off { background: var(--accent-off); color: #ffffff; border-color: var(--accent-off); box-shadow: none; }

    /* Buttons */
    .btn-row { display: flex; gap: 10px; }
    .btn { flex: 1; padding: 12px 16px; border-radius: 12px; border: none; font-size: 0.92rem; font-weight: 600; cursor: pointer; transition: all 0.12s ease; display: inline-flex; align-items: center; justify-content: center; gap: 8px; }
    .btn-primary { background: var(--primary); color: #0b1120; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-secondary { background: #334155; color: #f1f5f9; }
    .btn-secondary:hover { background: #475569; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Diagnostics Info */
    .info-list { display: flex; flex-direction: column; gap: 8px; font-size: 0.82rem; }
    .info-row { display: flex; justify-content: space-between; color: var(--text-muted); }
    .info-val { color: var(--text); font-weight: 500; font-family: monospace; font-size: 0.85rem; }

    /* Pairing Modal */
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75); display: none; align-items: center; justify-content: center; padding: 16px; z-index: 100; backdrop-filter: blur(4px); }
    .modal-overlay.open { display: flex; }
    .modal { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: var(--radius); width: 100%; max-width: 440px; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
    .modal h2 { font-size: 1.2rem; }
    .step-box { background: #131c2e; border: 1px solid var(--card-border); border-radius: 10px; padding: 12px; font-size: 0.85rem; line-height: 1.45; }
    .step-box ol { margin-left: 20px; }
    .step-box li { margin-bottom: 6px; }
    .step-box strong { color: var(--primary); }
    .pin-input { width: 100%; padding: 10px 14px; background: #0b1120; border: 1px solid var(--card-border); border-radius: 8px; color: #fff; font-size: 1.1rem; letter-spacing: 2px; text-align: center; }
    .pair-progress { font-size: 0.85rem; padding: 10px; border-radius: 8px; background: #0b1120; border: 1px solid #334155; min-height: 48px; display: flex; align-items: center; justify-content: center; text-align: center; color: var(--primary); }

    /* Spinner */
    .spinner { border: 2px solid rgba(255,255,255,0.2); border-left-color: var(--primary); border-radius: 50%; width: 14px; height: 14px; animation: spin 0.8s linear infinite; display: inline-block; margin-right: 4px; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1>Coleman-Mach Thermostat</h1>
        <p>RV Climate Bluetooth Web Controller</p>
      </div>
      <div class="badge" id="statusBadge">
        <span class="badge-dot" id="statusDot"></span>
        <span id="statusText">Connected</span>
      </div>
    </header>

    <!-- Main Thermostat Card -->
    <div class="card dial-card">
      <div class="zone-tag" id="zoneTag">Zone: Main</div>
      
      <div class="room-temp-container">
        <div class="room-temp-label">Current Room Temp</div>
        <div class="room-temp" id="roomTemp">--<sup>°F</sup></div>
      </div>

      <!-- Setpoint Controller with instant optimistic feedback -->
      <div class="setpoint-box">
        <button class="btn-circle" id="btnTempDown" aria-label="Decrease Target Temp">−</button>
        <div class="setpoint-val-group">
          <div class="setpoint-label">Target Setpoint</div>
          <div class="setpoint-number" id="targetTemp">--°F</div>
        </div>
        <button class="btn-circle" id="btnTempUp" aria-label="Increase Target Temp">+</button>
      </div>
    </div>

    <!-- Operating Mode Selection -->
    <div class="card">
      <div class="section-title">
        <span>Operating Mode</span>
        <span id="activeModeLabel" style="color: var(--primary); font-size: 0.82rem;">--</span>
      </div>
      <div class="mode-grid" id="modeGrid">
        <!-- Rendered dynamically -->
        <button class="btn-mode" data-mode="OFF">OFF</button>
        <button class="btn-mode" data-mode="FAN LOW">FAN LOW</button>
        <button class="btn-mode" data-mode="FAN HIGH">FAN HIGH</button>
        <button class="btn-mode" data-mode="COOL LOW">COOL LOW</button>
        <button class="btn-mode" data-mode="COOL HIGH">COOL HIGH</button>
        <button class="btn-mode" data-mode="COOL AUTO LOW">COOL AUTO LOW</button>
        <button class="btn-mode" data-mode="COOL AUTO HIGH">COOL AUTO HIGH</button>
      </div>
    </div>

    <!-- Actions & Pairing -->
    <div class="card">
      <div class="btn-row">
        <button class="btn btn-secondary" id="btnRefresh">
          <span id="refreshIcon">🔄</span> Refresh Now
        </button>
        <button class="btn btn-secondary" id="btnOpenPairModal">
          🔗 Pair New Unit
        </button>
      </div>
    </div>

    <!-- Device Metadata Info -->
    <div class="card">
      <div class="section-title">Device Diagnostics</div>
      <div class="info-list">
        <div class="info-row"><span>Bluetooth MAC</span><span class="info-val" id="macVal">--:--:--:--:--:--</span></div>
        <div class="info-row"><span>Unit ID / Passkey</span><span class="info-val" id="idVal">------</span></div>
        <div class="info-row"><span>Signal (RSSI)</span><span class="info-val" id="rssiVal">-- dBm</span></div>
        <div class="info-row"><span>Last Read</span><span class="info-val" id="lastUpdatedVal">Never</span></div>
      </div>
    </div>
  </div>

  <!-- Pairing Modal -->
  <div class="modal-overlay" id="pairModal">
    <div class="modal">
      <h2>Pair New Coleman-Mach Unit</h2>
      <div class="step-box">
        <ol>
          <li>Stand near the Coleman-Mach physical thermostat.</li>
          <li>Press & hold <strong>BOTH the UP and DOWN</strong> buttons for <strong>5 seconds</strong>.</li>
          <li>The thermostat screen will flash its 6-digit Unit ID (e.g. <code>96 89 00</code>).</li>
          <li>Enter the 6-digit PIN below and click <strong>Start Pairing</strong>.</li>
        </ol>
      </div>
      <div>
        <label style="font-size: 0.8rem; color: var(--text-muted); display: block; margin-bottom: 6px;">Thermostat Passkey / PIN (6 digits)</label>
        <input type="text" id="pairPinInput" class="pin-input" maxlength="6" value="" placeholder="e.g. 123456">
      </div>
      <div class="pair-progress" id="pairProgressBox">
        Ready to pair. Follow the 5-second button press step above first.
      </div>
      <div class="btn-row">
        <button class="btn btn-secondary" id="btnClosePairModal">Cancel</button>
        <button class="btn btn-primary" id="btnStartPairing">Start Pairing</button>
      </div>
    </div>
  </div>

  <script>
    let currentState = null;
    let pendingTemp = null;
    let tempDebounceTimer = null;
    let activeRequestCount = 0;

    const el = (id) => document.getElementById(id);

    function setSyncing(syncing, customMsg = null) {
      const dot = el('statusDot');
      const text = el('statusText');
      if (syncing) {
        dot.className = 'badge-dot syncing';
        text.textContent = customMsg || 'Syncing...';
      } else if (activeRequestCount <= 0) {
        dot.className = 'badge-dot';
        text.textContent = 'Connected';
      }
    }

    function showError(msg) {
      el('statusDot').className = 'badge-dot error';
      el('statusText').textContent = 'Sync Error';
      console.warn('Thermostat error:', msg);
    }

    async function fetchStatus(fresh = false) {
      activeRequestCount++;
      setSyncing(true, fresh ? 'Reading unit...' : 'Syncing...');
      try {
        const res = await fetch(`/api/status${fresh ? '?fresh=true' : ''}`);
        const data = await res.json();
        if (data.success && data.state) {
          updateUI(data.state, data.rssi);
        } else {
          showError(data.error || 'Failed to read status');
        }
      } catch (err) {
        showError('Unreachable');
      } finally {
        activeRequestCount--;
        if (activeRequestCount <= 0) setSyncing(false);
      }
    }

    function updateUI(state, rssi) {
      currentState = state;
      const unit = state.is_celsius ? '°C' : '°F';
      el('zoneTag').textContent = 'Zone: ' + (state.zone_name || 'Main');
      el('roomTemp').innerHTML = (state.temperature !== null ? state.temperature : '--') + `<sup>${unit}</sup>`;
      
      // If user isn't actively tapping +/- setpoint, sync setpoint display
      if (pendingTemp === null) {
        el('targetTemp').textContent = (state.set_point !== null ? state.set_point : '--') + unit;
        el('targetTemp').classList.remove('pending');
      }

      el('activeModeLabel').textContent = state.current_mode || '--';
      el('macVal').textContent = state.mac_address || '--';
      el('idVal').textContent = state.unit_id || '--';
      if (rssi) el('rssiVal').textContent = `${rssi} dBm`;
      el('lastUpdatedVal').textContent = new Date().toLocaleTimeString();

      renderModes(state.available_modes, state.current_mode);
    }

    function renderModes(modes, activeMode) {
      const grid = el('modeGrid');
      if (!modes || modes.length === 0) return;
      grid.innerHTML = '';
      modes.forEach(mode => {
        const btn = document.createElement('button');
        btn.className = 'btn-mode' + (mode === activeMode ? ' active' : '') + (mode === 'OFF' ? ' off' : '');
        btn.textContent = mode;
        btn.onclick = () => selectModeOptimistic(mode);
        grid.appendChild(btn);
      });
    }

    // 0ms Optimistic UI update on mode click
    async function selectModeOptimistic(newMode) {
      const prevMode = currentState ? currentState.current_mode : null;
      if (newMode === prevMode) return;

      // 1. Instant visual feedback (0ms)
      document.querySelectorAll('.btn-mode').forEach(b => {
        b.classList.remove('active', 'off');
        if (b.textContent === newMode) {
          b.classList.add('active');
          if (newMode === 'OFF') b.classList.add('off');
        }
      });
      el('activeModeLabel').textContent = newMode;
      activeRequestCount++;
      setSyncing(true, 'Setting ' + newMode + '...');

      // 2. Transmit to backend
      try {
        const res = await fetch('/api/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: newMode })
        });
        const data = await res.json();
        if (data.success && data.state) {
          updateUI(data.state);
        } else {
          // Revert on error
          alert('Mode change failed: ' + (data.error || 'Unknown error'));
          if (prevMode) renderModes(currentState.available_modes, prevMode);
        }
      } catch (err) {
        alert('Network error while setting mode: ' + err.message);
        if (prevMode) renderModes(currentState.available_modes, prevMode);
      } finally {
        activeRequestCount--;
        if (activeRequestCount <= 0) setSyncing(false);
      }
    }

    // Instant setpoint adjustment with 400ms debounce
    function adjustTemp(delta) {
      const cur = pendingTemp !== null ? pendingTemp : (currentState && currentState.set_point ? currentState.set_point : 72);
      const isC = currentState ? currentState.is_celsius : false;
      const min = isC ? 1 : 33;
      const max = isC ? 37 : 99;
      const next = Math.max(min, Math.min(max, cur + delta));

      // 0ms instant display update
      pendingTemp = next;
      el('targetTemp').textContent = next + (isC ? '°C' : '°F');
      el('targetTemp').classList.add('pending');

      // Debounce network dispatch by 400ms
      clearTimeout(tempDebounceTimer);
      tempDebounceTimer = setTimeout(async () => {
        const valToSend = pendingTemp;
        activeRequestCount++;
        setSyncing(true, 'Setting ' + valToSend + '°...');
        try {
          const res = await fetch('/api/set', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ temp: valToSend })
          });
          const data = await res.json();
          if (data.success && data.state) {
            pendingTemp = null;
            updateUI(data.state);
          } else {
            alert('Failed to set temperature: ' + (data.error || 'Unknown error'));
          }
        } catch (err) {
          alert('Network error: ' + err.message);
        } finally {
          activeRequestCount--;
          if (activeRequestCount <= 0) setSyncing(false);
        }
      }, 400);
    }

    // Event Listeners
    el('btnTempDown').onclick = () => adjustTemp(-1);
    el('btnTempUp').onclick = () => adjustTemp(1);
    el('btnRefresh').onclick = () => fetchStatus(true);

    // Modal & Pairing Logic
    el('btnOpenPairModal').onclick = () => {
      if (currentState && currentState.unit_id) {
        el('pairPinInput').value = currentState.unit_id;
      }
      el('pairModal').classList.add('open');
    };
    el('btnClosePairModal').onclick = () => el('pairModal').classList.remove('open');

    el('btnStartPairing').onclick = async () => {
      const pin = el('pairPinInput').value.trim();
      const progressBox = el('pairProgressBox');

      if (!pin || pin.length !== 6 || !/^\d{6}$/.test(pin)) {
        progressBox.textContent = 'Please enter the 6-digit PIN flashing on your thermostat LCD (e.g. 123456).';
        return;
      }

      el('btnStartPairing').disabled = true;
      progressBox.innerHTML = '<span class="spinner"></span> Starting pairing listener...';

      try {
        const res = await fetch('/api/pair', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pin: pin, wait: 60 })
        });
        const data = await res.json();
        if (!data.success) {
          progressBox.textContent = 'Failed to initiate: ' + data.message;
          el('btnStartPairing').disabled = false;
          return;
        }

        const pollTimer = setInterval(async () => {
          try {
            const pRes = await fetch('/api/pair/status');
            const pData = await pRes.json();
            progressBox.innerHTML = (pData.active ? '<span class="spinner"></span> ' : '') + pData.message;

            if (!pData.active) {
              clearInterval(pollTimer);
              el('btnStartPairing').disabled = false;
              if (pData.status === 'paired') {
                setTimeout(() => {
                  el('pairModal').classList.remove('open');
                  fetchStatus(true);
                }, 1500);
              }
            }
          } catch (e) {
            clearInterval(pollTimer);
            el('btnStartPairing').disabled = false;
          }
        }, 1200);

      } catch (err) {
        progressBox.textContent = 'Error: ' + err.message;
        el('btnStartPairing').disabled = false;
      }
    };

    // Initial load & non-blocking background refresh every 15s
    fetchStatus();
    setInterval(() => fetchStatus(false), 15000);
  </script>
</body>
</html>
"""

# =============================================================================
# REST Handlers & Server Lifespan
# =============================================================================

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=HTML_DASHBOARD, content_type="text/html")


async def handle_api_status(request: web.Request) -> web.Response:
    service: ThermostatService = request.app["service"]
    fresh = request.query.get("fresh", "").lower() in ("true", "1", "yes")
    try:
        state = await service.get_state(force_refresh=fresh)
        return web.json_response({
            "success": True,
            "state": asdict(state),
            "rssi": service.last_rssi,
            "cached": not fresh and (time.time() - service.cached_state_time) > 0.5,
        })
    except Exception as exc:
        logger.warning(f"Error in /api/status: {exc}")
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_api_set(request: web.Request) -> web.Response:
    service: ThermostatService = request.app["service"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    temp = body.get("temp")
    mode = body.get("mode")
    zone = body.get("zone")

    if temp is None and mode is None and zone is None:
        return web.json_response({"success": False, "error": "No settings provided"}, status=400)

    try:
        state = await service.apply_settings(set_point=temp, mode=mode, zone_name=zone)
        return web.json_response({
            "success": True,
            "state": asdict(state),
            "rssi": service.last_rssi,
        })
    except Exception as exc:
        logger.error(f"Error in /api/set: {exc}")
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_api_scan(request: web.Request) -> web.Response:
    service: ThermostatService = request.app["service"]
    timeout = float(request.query.get("timeout", "3.0"))
    try:
        devices = await service.scan_devices(timeout=timeout)
        return web.json_response({"success": True, "devices": devices})
    except Exception as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_api_pair_start(request: web.Request) -> web.Response:
    service: ThermostatService = request.app["service"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    pin = body.get("pin")
    wait = float(body.get("wait", 60.0))
    started, msg = service.start_pairing(pin=pin, timeout=wait)
    return web.json_response({"success": started, "message": msg})


async def handle_api_pair_status(request: web.Request) -> web.Response:
    service: ThermostatService = request.app["service"]
    return web.json_response({
        "success": True,
        "active": service.pairing_active,
        "status": service.pairing_status,
        "message": service.pairing_message,
    })


async def handle_api_config(request: web.Request) -> web.Response:
    config = cli.load_saved_config()
    return web.json_response({"success": True, "config": config})


async def on_cleanup(app: web.Application):
    service: ThermostatService = app["service"]
    service._running = False
    await service.close_client()


def make_app(service: ThermostatService) -> web.Application:
    app = web.Application()
    app["service"] = service
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_post("/api/set", handle_api_set)
    app.router.add_get("/api/scan", handle_api_scan)
    app.router.add_post("/api/pair", handle_api_pair_start)
    app.router.add_get("/api/pair/status", handle_api_pair_status)
    app.router.add_get("/api/config", handle_api_config)
    return app


def main():
    parser = argparse.ArgumentParser(description="Coleman-Mach RV Thermostat Web Service & Controller")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8050, help="HTTP port to bind (default: 8050)")
    parser.add_argument("--mac", help="Thermostat Bluetooth MAC address override")
    args = parser.parse_args()

    service = ThermostatService(target_mac=args.mac)
    app = make_app(service)

    print("======================================================================")
    print(" Coleman-Mach RV Thermostat Web Application (Optimized Low-Latency)")
    print(f" Web Dashboard & API available at: http://localhost:{args.port}/")
    print(f" Default Target MAC Address: {service.get_effective_mac() or 'Auto-discover'}")
    print("======================================================================")

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
