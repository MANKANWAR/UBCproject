#!/usr/bin/env python3
"""
Web GUI dashboard for UBCProject (Fan + SDS011 PM Sensor + PN532 NFC Haul Truck Detector).
Uses only the Python standard library for the web server (no Flask/extra installs needed).

Run on Raspberry Pi:
    python3 dashboard.py
Then open in any browser on the same network:
    http://<raspberry-pi-ip>:5000
"""

import argparse
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_DUTY = 20
HIGH_DUTY = 100
DEFAULT_PM25_THRESHOLD = 10.0
DEFAULT_PM10_THRESHOLD = 10.0
NFC_HOLD_SECONDS = 10.0


class SimulatedFan:
    def __init__(self, initial_duty=DEFAULT_DUTY):
        self._duty = initial_duty

    def set_duty(self, duty):
        self._duty = max(0, min(100, int(duty)))

    @property
    def duty(self):
        return self._duty

    def get_rpm(self):
        return round(self._duty * 28.0)

    def cleanup(self):
        pass


class SimulatedAirSensor:
    def __init__(self):
        self._t0 = time.monotonic()

    def start(self):
        pass

    def get_readings(self):
        t = time.monotonic() - self._t0
        pm25 = round(6.5 + 5.0 * math.sin(t / 6.0), 1)
        pm10 = round(pm25 + 2.1, 1)
        return pm25, pm10

    def stop(self):
        pass


class SimulatedNFC:
    def __init__(self):
        self._last = 0.0
        self._uid = ["0x04", "0xa2", "0x3f", "0x19"]

    def start(self):
        pass

    def trigger(self):
        self._last = time.monotonic()

    def is_active(self):
        if self._last == 0.0:
            return False, None
        return (time.monotonic() - self._last) <= NFC_HOLD_SECONDS, self._uid

    def stop(self):
        pass


class SystemController:
    def __init__(self, simulate=False):
        self.simulate = simulate
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self.mode = "AUTO"  # "AUTO" or "MANUAL"
        self.manual_duty = DEFAULT_DUTY
        self.pm25_threshold = DEFAULT_PM25_THRESHOLD
        self.pm10_threshold = DEFAULT_PM10_THRESHOLD

        self.pm25 = None
        self.pm10 = None
        self.nfc_active = False
        self.nfc_uid = None
        self.nfc_remaining = 0.0
        self._test_nfc_until = 0.0
        self._last_hw_nfc_seen = 0.0

        self.fan_duty = DEFAULT_DUTY
        self.fan_rpm = 0.0
        self.reason = "DEFAULT"
        self.hw_status = {"fan": "OK", "air": "OK", "nfc": "OK"}

        self.fan = None
        self.air = None
        self.nfc = None

        if simulate:
            self.fan = SimulatedFan(initial_duty=DEFAULT_DUTY)
            self.air = SimulatedAirSensor()
            self.nfc = SimulatedNFC()
            self.hw_status = {"fan": "SIM", "air": "SIM", "nfc": "SIM"}
        else:
            from main import AirQualitySensor, FanController, NFCMonitor

            try:
                self.fan = FanController(initial_duty=DEFAULT_DUTY)
            except Exception as e:
                print(f"[WARN] Fan init failed: {e}")
                self.hw_status["fan"] = f"ERR: {e}"

            try:
                self.air = AirQualitySensor()
                self.air.start()
            except Exception as e:
                print(f"[WARN] Air quality sensor init failed: {e}")
                self.hw_status["air"] = f"ERR: {e}"

            try:
                self.nfc = NFCMonitor()
                self.nfc.start()
            except Exception as e:
                print(f"[WARN] NFC init failed: {e}")
                self.hw_status["nfc"] = f"ERR: {e}"

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def trigger_test_nfc(self):
        now = time.monotonic()
        with self._lock:
            self._test_nfc_until = now + NFC_HOLD_SECONDS
        if self.simulate and self.nfc is not None:
            self.nfc.trigger()

    def set_thresholds(self, pm25_threshold=None, pm10_threshold=None):
        with self._lock:
            if pm25_threshold is not None:
                val = float(pm25_threshold)
                if val >= 0:
                    self.pm25_threshold = round(val, 1)
            if pm10_threshold is not None:
                val = float(pm10_threshold)
                if val >= 0:
                    self.pm10_threshold = round(val, 1)

    def _loop(self):
        last_rpm_sample = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            pm25, pm10 = (None, None)
            if self.air is not None:
                pm25, pm10 = self.air.get_readings()

            hw_nfc_active, uid = (False, None)
            if self.nfc is not None:
                hw_nfc_active, uid = self.nfc.is_active()

            with self._lock:
                mode = self.mode
                manual_duty = self.manual_duty
                pm25_thresh = self.pm25_threshold
                pm10_thresh = self.pm10_threshold
                test_remaining = max(0.0, self._test_nfc_until - now)

            if hw_nfc_active:
                self._last_hw_nfc_seen = now

            hw_remaining = (
                max(0.0, NFC_HOLD_SECONDS - (now - self._last_hw_nfc_seen))
                if hw_nfc_active
                else 0.0
            )
            nfc_active = hw_nfc_active or (test_remaining > 0.0)
            nfc_remaining = max(hw_remaining, test_remaining)
            if nfc_active and uid is None:
                uid = ["0x04", "0xa2", "0x3f", "0x19"]

            pm25_high = pm25 is not None and pm25 >= pm25_thresh
            pm10_high = pm10 is not None and pm10 >= pm10_thresh

            if mode == "MANUAL":
                target_duty = manual_duty
                reason = "MANUAL OVERRIDE"
            else:
                reasons = []
                if nfc_active:
                    reasons.append("HAUL TRUCK DETECTED")
                if pm25_high:
                    reasons.append(f"PM2.5 >= {pm25_thresh:g}")
                if pm10_high:
                    reasons.append(f"PM10 >= {pm10_thresh:g}")
                target_duty = HIGH_DUTY if reasons else DEFAULT_DUTY
                reason = " + ".join(reasons) if reasons else "DEFAULT"

            if self.fan is not None:
                self.fan.set_duty(target_duty)

            rpm_val = None
            if self.fan is not None and (now - last_rpm_sample) >= 1.0:
                rpm_val = self.fan.get_rpm()
                last_rpm_sample = now

            with self._lock:
                self.pm25 = pm25
                self.pm10 = pm10
                self.nfc_active = nfc_active
                self.nfc_uid = uid if nfc_active else None
                self.nfc_remaining = round(nfc_remaining, 1)
                self.fan_duty = self.fan.duty if self.fan is not None else target_duty
                if rpm_val is not None:
                    self.fan_rpm = rpm_val
                self.reason = reason

            time.sleep(0.25)

    def set_fan_control(self, mode=None, duty=None, delta=None):
        with self._lock:
            if mode == "AUTO":
                self.mode = "AUTO"
            elif delta is not None:
                base = self.manual_duty if self.mode == "MANUAL" else self.fan_duty
                self.mode = "MANUAL"
                self.manual_duty = max(0, min(100, int(base) + int(delta)))
            elif duty is not None:
                self.mode = "MANUAL"
                self.manual_duty = max(0, min(100, int(duty)))
            elif mode == "MANUAL":
                self.mode = "MANUAL"

    def get_status(self):
        with self._lock:
            pm25_high = self.pm25 is not None and self.pm25 >= self.pm25_threshold
            pm10_high = self.pm10 is not None and self.pm10 >= self.pm10_threshold
            return {
                "pm25": self.pm25,
                "pm10": self.pm10,
                "pm25_threshold": self.pm25_threshold,
                "pm10_threshold": self.pm10_threshold,
                "pm25_high": pm25_high,
                "pm10_high": pm10_high,
                "pm_high": pm25_high or pm10_high,
                "nfc_active": self.nfc_active,
                "nfc_uid": self.nfc_uid,
                "nfc_remaining": self.nfc_remaining,
                "nfc_hold_seconds": NFC_HOLD_SECONDS,
                "fan_duty": self.fan_duty,
                "fan_rpm": round(self.fan_rpm),
                "mode": self.mode,
                "manual_duty": self.manual_duty,
                "reason": self.reason,
                "hw_status": self.hw_status,
                "simulate": self.simulate,
            }

    def stop(self):
        self._stop_event.set()
        if self.air is not None:
            self.air.stop()
        if self.nfc is not None:
            self.nfc.stop()
        if self.fan is not None:
            self.fan.cleanup()


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>UBC Mine Ventilation Control</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --green: #22c55e;
      --amber: #f59e0b;
      --red: #ef4444;
      --blue: #38bdf8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 20px;
      min-height: 100vh;
    }
    .container { max-width: 980px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 18px;
      flex-wrap: wrap;
      gap: 12px;
    }
    h1 { font-size: 1.45rem; font-weight: 700; }
    .header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .conn-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: var(--card);
      border: 1px solid var(--border);
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 0.85rem;
      color: var(--muted);
    }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--green); }
    .dot.offline { background: var(--red); }

    /* Haul Truck Banner + Tunnel Simulation */
    .tunnel-card {
      background: var(--card);
      border: 2px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
      margin-bottom: 20px;
      transition: border-color 0.25s, box-shadow 0.25s;
    }
    .tunnel-card.detected {
      border-color: var(--amber);
      box-shadow: 0 0 24px rgba(245, 158, 11, 0.18);
    }
    .tunnel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .truck-title { font-size: 1.25rem; font-weight: 700; }
    .truck-sub { color: var(--muted); font-size: 0.88rem; margin-top: 3px; }
    .badge {
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      background: rgba(34, 197, 94, 0.15);
      color: var(--green);
    }
    .badge.alert { background: var(--amber); color: #0f172a; }
    .badge.danger { background: var(--red); color: #fff; }

    .tunnel-viewport {
      width: 100%;
      background: #0b1120;
      border: 1px solid #1e293b;
      border-radius: 10px;
      overflow: hidden;
    }
    .tunnel-svg { width: 100%; height: auto; display: block; }

    /* Animated Haul Truck */
    #haulTruckGroup {
      transform: translateX(840px);
      transition: transform 1.6s cubic-bezier(0.22, 1, 0.36, 1);
    }
    .tunnel-card.detected #haulTruckGroup {
      transform: translateX(370px);
    }
    .headlight-beam {
      opacity: 0;
      transition: opacity 0.4s ease;
    }
    .tunnel-card.detected .headlight-beam {
      opacity: 0.42;
    }
    .exhaust-puff {
      opacity: 0;
    }
    .tunnel-card.detected .exhaust-puff {
      animation: puff 1.4s ease-out infinite;
    }
    .tunnel-card.detected .exhaust-puff.p2 { animation-delay: 0.45s; }
    .tunnel-card.detected .exhaust-puff.p3 { animation-delay: 0.9s; }
    @keyframes puff {
      0% { opacity: 0.75; transform: translate(0, 0) scale(0.6); }
      100% { opacity: 0; transform: translate(35px, -24px) scale(1.7); }
    }

    /* Spinning Fan Blades & Airflow */
    #fanBlades {
      transform-origin: 62px 92px;
      animation: spin var(--fan-spin-dur, 1.8s) linear infinite;
    }
    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    .airflow-line {
      stroke-dasharray: 18 22;
      animation: flow var(--air-flow-dur, 1.5s) linear infinite;
    }
    @keyframes flow {
      from { stroke-dashoffset: 40; }
      to { stroke-dashoffset: 0; }
    }

    /* Dust Haze & Drifting PM Particles */
    #dustHaze {
      transition: opacity 0.6s ease;
    }
    #dustLayer {
      transition: opacity 0.6s ease;
    }
    .dust-dot {
      animation: dustDrift var(--dust-dur, 4.5s) linear infinite;
    }
    .dust-dot.d2 { animation-delay: -1.1s; }
    .dust-dot.d3 { animation-delay: -2.2s; }
    .dust-dot.d4 { animation-delay: -3.3s; }
    .dust-dot.d5 { animation-delay: -0.6s; }
    @keyframes dustDrift {
      0%   { transform: translate(0px, 0px); opacity: 0; }
      15%  { opacity: 0.9; }
      50%  { transform: translate(110px, -10px); opacity: 0.85; }
      85%  { opacity: 0.7; }
      100% { transform: translate(220px, 6px); opacity: 0; }
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 20px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }
    .card-title {
      font-size: 0.88rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      font-weight: 600;
    }
    .metrics-row { display: flex; gap: 18px; margin-bottom: 14px; }
    .metric { flex: 1; }
    .metric-label { font-size: 0.85rem; color: var(--muted); }
    .metric-value {
      font-size: 2.2rem;
      font-weight: 700;
      margin-top: 2px;
      font-variant-numeric: tabular-nums;
    }
    .unit { font-size: 0.88rem; color: var(--muted); font-weight: 400; }
    .high { color: var(--red); }

    .threshold-box {
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .threshold-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .threshold-row label {
      font-size: 0.86rem;
      color: var(--muted);
      min-width: 120px;
    }
    .threshold-inputs {
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
    }
    input[type="number"] {
      width: 74px;
      background: #0f172a;
      color: var(--text);
      border: 1px solid #475569;
      border-radius: 6px;
      padding: 6px 8px;
      font-size: 0.9rem;
      font-weight: 600;
      text-align: center;
    }
    input[type="range"] { width: 100%; accent-color: var(--blue); cursor: pointer; }

    .bar-bg {
      width: 100%;
      height: 12px;
      background: #0f172a;
      border-radius: 999px;
      overflow: hidden;
      margin: 12px 0;
    }
    .bar-fill {
      height: 100%;
      width: 20%;
      background: linear-gradient(90deg, var(--blue), #6366f1);
      transition: width 0.25s ease;
    }
    .controls { margin-top: 14px; display: flex; flex-direction: column; gap: 12px; }
    .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
    button {
      background: #334155;
      color: var(--text);
      border: 1px solid #475569;
      padding: 8px 13px;
      border-radius: 8px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s, transform 0.05s;
    }
    button:hover { background: #475569; }
    button:active { transform: scale(0.97); }
    button.primary { background: #0284c7; border-color: #38bdf8; }
    button.primary:hover { background: #0369a1; }
    button.amber { background: #d97706; border-color: #f59e0b; color: #fff; }
    button.amber:hover { background: #b45309; }
    button.auto-active { background: #16a34a; border-color: #22c55e; }
    .save-msg { font-size: 0.8rem; color: var(--green); min-height: 1em; }

    .footer-info {
      margin-top: 16px;
      font-size: 0.8rem;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>UBC Mine Ventilation Monitor</h1>
      <div class="header-right">
        <button class="amber" onclick="simNfc()">Simulate NFC Haul Truck</button>
        <div class="conn-pill">
          <span class="dot" id="connDot"></span>
          <span id="connText">Connecting...</span>
        </div>
      </div>
    </header>

    <!-- Haul Truck Detection + Live Mine Tunnel Simulation -->
    <div class="tunnel-card" id="truckBanner">
      <div class="tunnel-header">
        <div>
          <div class="truck-title" id="truckTitle">No Haul Truck Detected</div>
          <div class="truck-sub" id="truckSub">Monitoring NFC tag reader at tunnel entrance...</div>
        </div>
        <span class="badge" id="truckBadge">TUNNEL CLEAR</span>
      </div>

      <div class="tunnel-viewport">
        <svg class="tunnel-svg" viewBox="0 0 800 170" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="dustGrad" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stop-color="#d97706" stop-opacity="0.15" />
              <stop offset="50%" stop-color="#f59e0b" stop-opacity="0.55" />
              <stop offset="100%" stop-color="#b45309" stop-opacity="0.35" />
            </linearGradient>
          </defs>
          <!-- Tunnel ceiling & floor -->
          <rect x="0" y="0" width="800" height="22" fill="#1e293b" />
          <rect x="0" y="142" width="800" height="28" fill="#1e293b" />
          <!-- Dust haze overlay (opacity scales with PM2.5 / PM10) -->
          <rect id="dustHaze" x="0" y="22" width="800" height="120" fill="url(#dustGrad)" opacity="0" />
          <line x1="0" y1="22" x2="800" y2="22" stroke="#334155" stroke-width="2" />
          <line x1="0" y1="142" x2="800" y2="142" stroke="#475569" stroke-width="2" />
          <!-- Dashed road center line -->
          <line x1="120" y1="156" x2="780" y2="156" stroke="#f59e0b" stroke-width="2" stroke-dasharray="16 14" opacity="0.55" />

          <!-- Floating / Blowing Dust Particles (PM2.5 fine + PM10 coarse) -->
          <g id="dustLayer" opacity="0" fill="#fbbf24">
            <circle class="dust-dot" cx="140" cy="42" r="2.2" />
            <circle class="dust-dot d2" cx="195" cy="78" r="3.4" />
            <circle class="dust-dot d3" cx="230" cy="118" r="2.5" />
            <circle class="dust-dot d4" cx="275" cy="54" r="3.8" />
            <circle class="dust-dot d5" cx="315" cy="96" r="2.0" />
            <circle class="dust-dot" cx="360" cy="40" r="3.2" />
            <circle class="dust-dot d2" cx="405" cy="126" r="2.8" />
            <circle class="dust-dot d3" cx="445" cy="72" r="4.0" />
            <circle class="dust-dot d4" cx="490" cy="108" r="2.4" />
            <circle class="dust-dot d5" cx="535" cy="48" r="3.5" />
            <circle class="dust-dot" cx="575" cy="88" r="2.6" />
            <circle class="dust-dot d2" cx="615" cy="122" r="3.2" />
            <circle class="dust-dot d3" cx="165" cy="104" r="1.8" />
            <circle class="dust-dot d4" cx="255" cy="84" r="2.2" />
            <circle class="dust-dot d5" cx="345" cy="66" r="1.9" />
            <circle class="dust-dot" cx="430" cy="46" r="2.5" />
            <circle class="dust-dot d2" cx="515" cy="94" r="3.0" />
            <circle class="dust-dot d3" cx="595" cy="60" r="2.1" />
          </g>
          <text id="dustAlertLabel" x="400" y="38" text-anchor="middle" fill="#fbbf24" font-size="11" font-weight="800" opacity="0">HIGH DUST LEVEL — CLEARING TUNNEL</text>

          <!-- NFC Checkpoint Arch -->
          <rect x="650" y="22" width="12" height="120" fill="#334155" />
          <rect x="644" y="26" width="24" height="14" rx="3" fill="#0ea5e9" id="nfcGateLight" />
          <text x="656" y="16" text-anchor="middle" fill="#94a3b8" font-size="10" font-weight="700">NFC GATE</text>

          <!-- Ventilation Fan Housing (Left Wall) -->
          <rect x="20" y="50" width="84" height="84" rx="10" fill="#1e293b" stroke="#475569" stroke-width="2" />
          <circle cx="62" cy="92" r="32" fill="#0f172a" stroke="#38bdf8" stroke-width="2" />
          <g id="fanBlades">
            <path d="M62 92 L54 64 Q62 58 70 64 Z" fill="#38bdf8" />
            <path d="M62 92 L90 84 Q96 92 90 100 Z" fill="#38bdf8" />
            <path d="M62 92 L70 120 Q62 126 54 120 Z" fill="#38bdf8" />
            <path d="M62 92 L34 100 Q28 92 34 84 Z" fill="#38bdf8" />
            <circle cx="62" cy="92" r="6" fill="#f8fafc" />
          </g>
          <text x="62" y="42" text-anchor="middle" fill="#38bdf8" font-size="10" font-weight="700" id="svgFanLabel">FAN 20%</text>

          <!-- Airflow Streams -->
          <g id="airflowGroup" stroke="#38bdf8" stroke-width="2.5" stroke-linecap="round" opacity="0.45">
            <line class="airflow-line" x1="115" y1="68" x2="340" y2="68" />
            <line class="airflow-line" x1="115" y1="92" x2="360" y2="92" />
            <line class="airflow-line" x1="115" y1="116" x2="340" y2="116" />
          </g>

          <!-- Haul Truck (slides in from right when NFC detected) -->
          <g id="haulTruckGroup">
            <!-- Headlight beam pointing left into tunnel -->
            <polygon class="headlight-beam" points="10,104 -95,82 -95,134 10,118" fill="#fef08a" />
            <!-- Exhaust puffs -->
            <circle class="exhaust-puff p1" cx="72" cy="52" r="7" fill="#94a3b8" />
            <circle class="exhaust-puff p2" cx="76" cy="48" r="9" fill="#94a3b8" />
            <circle class="exhaust-puff p3" cx="80" cy="44" r="6" fill="#94a3b8" />
            <!-- Exhaust stack -->
            <rect x="66" y="54" width="6" height="24" rx="2" fill="#64748b" />
            <!-- Ore load inside dump bed -->
            <polygon points="82,68 112,52 152,55 182,68" fill="#64748b" />
            <!-- Dump bed -->
            <polygon points="74,66 196,66 206,106 74,106" fill="#f59e0b" stroke="#b45309" stroke-width="2" />
            <line x1="108" y1="68" x2="108" y2="104" stroke="#b45309" stroke-width="2" />
            <line x1="144" y1="68" x2="144" y2="104" stroke="#b45309" stroke-width="2" />
            <line x1="178" y1="68" x2="178" y2="104" stroke="#b45309" stroke-width="2" />
            <!-- Cab -->
            <rect x="14" y="72" width="56" height="36" rx="5" fill="#fbbf24" stroke="#b45309" stroke-width="2" />
            <!-- Windshield -->
            <rect x="20" y="78" width="24" height="16" rx="2" fill="#0f172a" />
            <!-- Headlight -->
            <rect x="10" y="96" width="6" height="9" rx="1" fill="#fef08a" />
            <!-- Chassis -->
            <rect x="12" y="106" width="192" height="14" rx="3" fill="#334155" />
            <!-- Front & Rear Heavy Wheels -->
            <circle cx="48" cy="126" r="16" fill="#0f172a" stroke="#64748b" stroke-width="4" />
            <circle cx="48" cy="126" r="6" fill="#f59e0b" />
            <circle cx="164" cy="126" r="16" fill="#0f172a" stroke="#64748b" stroke-width="4" />
            <circle cx="164" cy="126" r="6" fill="#f59e0b" />
            <text x="138" y="92" text-anchor="middle" fill="#0f172a" font-size="11" font-weight="800">HAUL TRUCK</text>
          </g>
        </svg>
      </div>
    </div>

    <div class="grid">
      <!-- Air Quality Card + Separate PM2.5 / PM10 Threshold Controls -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Air Quality (PM Sensor)</span>
          <span class="badge" id="pmBadge">NORMAL</span>
        </div>
        <div class="metrics-row">
          <div class="metric">
            <div class="metric-label">PM2.5</div>
            <div class="metric-value" id="pm25">--</div>
            <div class="unit">&micro;g/m&sup3; (Limit: <span id="pm25LimitText">10</span>)</div>
          </div>
          <div class="metric">
            <div class="metric-label">PM10</div>
            <div class="metric-value" id="pm10">--</div>
            <div class="unit">&micro;g/m&sup3; (Limit: <span id="pm10LimitText">10</span>)</div>
          </div>
        </div>

        <div class="threshold-box">
          <div class="card-title">PM Auto-Boost Thresholds</div>
          <div class="threshold-row">
            <label for="pm25ThreshInput">PM2.5 Limit (&micro;g/m&sup3;)</label>
            <div class="threshold-inputs">
              <input type="range" id="pm25ThreshSlider" min="1" max="100" step="1" value="10" />
              <input type="number" id="pm25ThreshInput" min="0" max="500" step="0.5" value="10" />
            </div>
          </div>
          <div class="threshold-row">
            <label for="pm10ThreshInput">PM10 Limit (&micro;g/m&sup3;)</label>
            <div class="threshold-inputs">
              <input type="range" id="pm10ThreshSlider" min="1" max="150" step="1" value="10" />
              <input type="number" id="pm10ThreshInput" min="0" max="500" step="0.5" value="10" />
            </div>
          </div>
          <div class="btn-row" style="align-items: center;">
            <button class="primary" onclick="saveThresholds()">Apply Thresholds</button>
            <span class="save-msg" id="threshMsg"></span>
          </div>
        </div>
      </div>

      <!-- Fan Speed & Control Card -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Ventilation Fan</span>
          <span class="badge" id="modeBadge">AUTO</span>
        </div>
        <div class="metrics-row">
          <div class="metric">
            <div class="metric-label">Duty Cycle</div>
            <div class="metric-value"><span id="fanDuty">20</span><span class="unit">%</span></div>
          </div>
          <div class="metric">
            <div class="metric-label">Fan Speed</div>
            <div class="metric-value"><span id="fanRpm">0</span> <span class="unit">RPM</span></div>
          </div>
        </div>
        <div class="bar-bg"><div class="bar-fill" id="fanBar"></div></div>
        <div class="truck-sub">Active trigger: <strong id="reasonText">DEFAULT</strong></div>

        <div class="controls">
          <input type="range" id="fanSlider" min="0" max="100" step="5" value="20" />
          <div class="btn-row">
            <button onclick="adjustFan(-10)">-10%</button>
            <button class="primary" onclick="adjustFan(10)">+10% Increase</button>
            <button onclick="setFanDuty(20)">20%</button>
            <button onclick="setFanDuty(100)">100%</button>
            <button id="autoBtn" onclick="setAuto()">Auto Mode</button>
          </div>
        </div>
      </div>
    </div>

    <div class="footer-info">
      <span id="hwStatus">Hardware: checking...</span>
    </div>
  </div>

  <script>
    const fanSlider = document.getElementById("fanSlider");
    let fanSliderDragging = false;
    fanSlider.addEventListener("pointerdown", () => fanSliderDragging = true);
    fanSlider.addEventListener("pointerup", () => {
      fanSliderDragging = false;
      setFanDuty(parseInt(fanSlider.value, 10));
    });
    fanSlider.addEventListener("change", () => setFanDuty(parseInt(fanSlider.value, 10)));

    // Sync PM threshold sliders & number inputs
    const pm25Slider = document.getElementById("pm25ThreshSlider");
    const pm25Input = document.getElementById("pm25ThreshInput");
    const pm10Slider = document.getElementById("pm10ThreshSlider");
    const pm10Input = document.getElementById("pm10ThreshInput");
    let threshInitialized = false;

    pm25Slider.addEventListener("input", () => pm25Input.value = pm25Slider.value);
    pm25Input.addEventListener("input", () => pm25Slider.value = pm25Input.value);
    pm10Slider.addEventListener("input", () => pm10Input.value = pm10Slider.value);
    pm10Input.addEventListener("input", () => pm10Slider.value = pm10Input.value);

    async function postFan(payload) {
      await fetch("/api/fan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      poll();
    }

    function setFanDuty(duty) { postFan({ mode: "MANUAL", duty }); }
    function adjustFan(delta) { postFan({ delta }); }
    function setAuto() { postFan({ mode: "AUTO" }); }

    async function simNfc() {
      await fetch("/api/sim-nfc", { method: "POST" });
      poll();
    }

    async function saveThresholds() {
      const pm25_threshold = parseFloat(pm25Input.value);
      const pm10_threshold = parseFloat(pm10Input.value);
      if (isNaN(pm25_threshold) || isNaN(pm10_threshold) || pm25_threshold < 0 || pm10_threshold < 0) {
        return;
      }
      await fetch("/api/thresholds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pm25_threshold, pm10_threshold }),
      });
      const msg = document.getElementById("threshMsg");
      msg.textContent = "Saved!";
      setTimeout(() => { msg.textContent = ""; }, 2000);
      poll();
    }

    async function poll() {
      try {
        const res = await fetch("/api/status");
        const d = await res.json();

        document.getElementById("connDot").classList.remove("offline");
        document.getElementById("connText").textContent = d.simulate ? "Connected (Simulation)" : "Connected (Live)";

        // Initialize threshold inputs once from server
        if (!threshInitialized) {
          pm25Input.value = d.pm25_threshold;
          pm25Slider.value = d.pm25_threshold;
          pm10Input.value = d.pm10_threshold;
          pm10Slider.value = d.pm10_threshold;
          threshInitialized = true;
        }
        document.getElementById("pm25LimitText").textContent = d.pm25_threshold;
        document.getElementById("pm10LimitText").textContent = d.pm10_threshold;

        // Haul Truck / NFC + Tunnel Animation
        const banner = document.getElementById("truckBanner");
        const truckTitle = document.getElementById("truckTitle");
        const truckSub = document.getElementById("truckSub");
        const truckBadge = document.getElementById("truckBadge");
        const gateLight = document.getElementById("nfcGateLight");

        if (d.nfc_active) {
          banner.classList.add("detected");
          truckTitle.textContent = "HAUL TRUCK DETECTED — ENTERING TUNNEL";
          truckSub.textContent =
            "NFC Tag UID: " + (d.nfc_uid ? d.nfc_uid.join(" ") : "Detected") +
            `  |  Boost hold: ${d.nfc_remaining.toFixed(1)}s`;
          truckBadge.textContent = "TRUCK IN TUNNEL";
          truckBadge.className = "badge alert";
          gateLight.setAttribute("fill", "#f59e0b");
        } else {
          banner.classList.remove("detected");
          truckTitle.textContent = "No Haul Truck Detected";
          truckSub.textContent = "Monitoring NFC tag reader at tunnel entrance...";
          truckBadge.textContent = "TUNNEL CLEAR";
          truckBadge.className = "badge";
          gateLight.setAttribute("fill", "#0ea5e9");
        }

        // Update SVG fan blade spin speed & airflow speed
        const rootStyle = document.documentElement.style;
        if (d.fan_duty <= 0) {
          rootStyle.setProperty("--fan-spin-dur", "999s");
          rootStyle.setProperty("--air-flow-dur", "999s");
          document.getElementById("airflowGroup").setAttribute("opacity", "0.1");
        } else {
          const spinDur = Math.max(0.18, 2.2 - (d.fan_duty / 100) * 2.02);
          const flowDur = Math.max(0.25, 1.8 - (d.fan_duty / 100) * 1.55);
          rootStyle.setProperty("--fan-spin-dur", spinDur.toFixed(2) + "s");
          rootStyle.setProperty("--air-flow-dur", flowDur.toFixed(2) + "s");
          document.getElementById("airflowGroup").setAttribute("opacity", d.fan_duty >= 80 ? "0.95" : "0.45");
        }
        document.getElementById("svgFanLabel").textContent = `FAN ${d.fan_duty}%`;

        // Update dust haze & particle animation based on PM levels
        const pm25Ratio = d.pm25 !== null && d.pm25_threshold > 0 ? d.pm25 / d.pm25_threshold : 0;
        const pm10Ratio = d.pm10 !== null && d.pm10_threshold > 0 ? d.pm10 / d.pm10_threshold : 0;
        const dustLevel = Math.max(0, Math.min(1.5, Math.max(pm25Ratio, pm10Ratio)));
        const dustLayer = document.getElementById("dustLayer");
        const dustHaze = document.getElementById("dustHaze");
        const dustAlertLabel = document.getElementById("dustAlertLabel");

        if (dustLevel <= 0.25) {
          dustLayer.setAttribute("opacity", "0");
          dustHaze.setAttribute("opacity", "0");
          dustAlertLabel.setAttribute("opacity", "0");
        } else {
          const particleOpacity = Math.min(1.0, (dustLevel - 0.2) * 0.95);
          const hazeOpacity = Math.min(0.65, (dustLevel - 0.25) * 0.55);
          dustLayer.setAttribute("opacity", particleOpacity.toFixed(2));
          dustLayer.setAttribute("fill", d.pm_high ? "#fb923c" : "#cbd5e1");
          dustHaze.setAttribute("opacity", hazeOpacity.toFixed(2));
          dustAlertLabel.setAttribute("opacity", d.pm_high ? "0.95" : "0");
          const dustDur = Math.max(1.1, 5.2 - (d.fan_duty / 100) * 3.8);
          rootStyle.setProperty("--dust-dur", dustDur.toFixed(2) + "s");
        }

        // PM values
        const pm25El = document.getElementById("pm25");
        const pm10El = document.getElementById("pm10");
        pm25El.textContent = d.pm25 !== null ? d.pm25.toFixed(1) : "--";
        pm10El.textContent = d.pm10 !== null ? d.pm10.toFixed(1) : "--";
        pm25El.classList.toggle("high", d.pm25_high);
        pm10El.classList.toggle("high", d.pm10_high);

        const pmBadge = document.getElementById("pmBadge");
        if (d.pm25_high && d.pm10_high) {
          pmBadge.textContent = "HIGH PM2.5 + PM10";
          pmBadge.className = "badge danger";
        } else if (d.pm25_high) {
          pmBadge.textContent = "HIGH PM2.5";
          pmBadge.className = "badge danger";
        } else if (d.pm10_high) {
          pmBadge.textContent = "HIGH PM10";
          pmBadge.className = "badge danger";
        } else {
          pmBadge.textContent = "NORMAL";
          pmBadge.className = "badge";
        }

        // Fan card
        document.getElementById("fanDuty").textContent = d.fan_duty;
        document.getElementById("fanRpm").textContent = d.fan_rpm;
        document.getElementById("fanBar").style.width = d.fan_duty + "%";
        document.getElementById("reasonText").textContent = d.reason;
        if (!fanSliderDragging) fanSlider.value = d.fan_duty;

        const modeBadge = document.getElementById("modeBadge");
        modeBadge.textContent = d.mode;
        modeBadge.className = d.mode === "MANUAL" ? "badge alert" : "badge";
        document.getElementById("autoBtn").className = d.mode === "AUTO" ? "auto-active" : "";

        document.getElementById("hwStatus").textContent =
          `Fan: ${d.hw_status.fan} | Air Sensor: ${d.hw_status.air} | NFC: ${d.hw_status.nfc}`;
      } catch (e) {
        document.getElementById("connDot").classList.add("offline");
        document.getElementById("connText").textContent = "Disconnected";
      }
    }

    poll();
    setInterval(poll, 500);
  </script>
</body>
</html>
"""


def make_handler(controller):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, obj, status=200):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                body = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                self._send_json(controller.get_status())
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json"}, status=400)
                return

            if self.path == "/api/fan":
                controller.set_fan_control(
                    mode=payload.get("mode"),
                    duty=payload.get("duty"),
                    delta=payload.get("delta"),
                )
                self._send_json(controller.get_status())
            elif self.path == "/api/thresholds":
                controller.set_thresholds(
                    pm25_threshold=payload.get("pm25_threshold"),
                    pm10_threshold=payload.get("pm10_threshold"),
                )
                self._send_json(controller.get_status())
            elif self.path == "/api/sim-nfc":
                controller.trigger_test_nfc()
                self._send_json(controller.get_status())
            else:
                self.send_error(404)

    return DashboardHandler


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    parser = argparse.ArgumentParser(description="UBCProject Web GUI Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="HTTP port (default: 5000)")
    parser.add_argument("--simulate", action="store_true", help="Run with simulated sensors/fan")
    args = parser.parse_args()

    controller = SystemController(simulate=args.simulate)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(controller))

    ip = get_local_ip()
    print("Dashboard running at:")
    print(f"  Local:   http://localhost:{args.port}")
    print(f"  Network: http://{ip}:{args.port}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
    finally:
        server.server_close()
        controller.stop()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
