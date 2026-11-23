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
PM_THRESHOLD = 10.0
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
        self.pm25 = None
        self.pm10 = None
        self.nfc_active = False
        self.nfc_uid = None
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

    def _loop(self):
        last_rpm_sample = time.monotonic()
        while not self._stop_event.is_set():
            pm25, pm10 = (None, None)
            if self.air is not None:
                pm25, pm10 = self.air.get_readings()

            nfc_active, uid = (False, None)
            if self.nfc is not None:
                nfc_active, uid = self.nfc.is_active()

            pm_high = (
                (pm25 is not None and pm25 >= PM_THRESHOLD)
                or (pm10 is not None and pm10 >= PM_THRESHOLD)
            )

            with self._lock:
                mode = self.mode
                manual_duty = self.manual_duty

            if mode == "MANUAL":
                target_duty = manual_duty
                reason = "MANUAL OVERRIDE"
            else:
                reasons = []
                if nfc_active:
                    reasons.append("HAUL TRUCK DETECTED")
                if pm_high:
                    reasons.append(f"PM >= {PM_THRESHOLD:.0f}")
                target_duty = HIGH_DUTY if reasons else DEFAULT_DUTY
                reason = " + ".join(reasons) if reasons else "DEFAULT"

            if self.fan is not None:
                self.fan.set_duty(target_duty)

            now = time.monotonic()
            rpm_val = None
            if self.fan is not None and (now - last_rpm_sample) >= 1.0:
                rpm_val = self.fan.get_rpm()
                last_rpm_sample = now

            with self._lock:
                self.pm25 = pm25
                self.pm10 = pm10
                self.nfc_active = nfc_active
                self.nfc_uid = uid
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
            pm_high = (
                (self.pm25 is not None and self.pm25 >= PM_THRESHOLD)
                or (self.pm10 is not None and self.pm10 >= PM_THRESHOLD)
            )
            return {
                "pm25": self.pm25,
                "pm10": self.pm10,
                "pm_threshold": PM_THRESHOLD,
                "pm_high": pm_high,
                "nfc_active": self.nfc_active,
                "nfc_uid": self.nfc_uid,
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
  <title>UBC Ventilation Control</title>
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
      padding: 24px;
      min-height: 100vh;
    }
    .container { max-width: 960px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
      flex-wrap: wrap;
      gap: 12px;
    }
    h1 { font-size: 1.5rem; font-weight: 700; }
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
    .dot {
      width: 9px; height: 9px; border-radius: 50%;
      background: var(--green);
    }
    .dot.offline { background: var(--red); }

    .truck-banner {
      background: var(--card);
      border: 2px solid var(--border);
      border-radius: 14px;
      padding: 20px 24px;
      margin-bottom: 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      transition: border-color 0.25s, background 0.25s;
    }
    .truck-banner.detected {
      border-color: var(--amber);
      background: rgba(245, 158, 11, 0.14);
    }
    .truck-title {
      font-size: 1.35rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .truck-sub { color: var(--muted); font-size: 0.9rem; margin-top: 4px; }
    .badge {
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      background: rgba(34, 197, 94, 0.15);
      color: var(--green);
    }
    .badge.alert {
      background: var(--amber);
      color: #0f172a;
    }
    .badge.danger {
      background: var(--red);
      color: #fff;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
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
      margin-bottom: 16px;
    }
    .card-title {
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      font-weight: 600;
    }
    .metrics-row {
      display: flex;
      gap: 20px;
      margin-bottom: 14px;
    }
    .metric { flex: 1; }
    .metric-label { font-size: 0.85rem; color: var(--muted); }
    .metric-value {
      font-size: 2.3rem;
      font-weight: 700;
      margin-top: 2px;
      font-variant-numeric: tabular-nums;
    }
    .unit { font-size: 0.9rem; color: var(--muted); font-weight: 400; }
    .high { color: var(--red); }

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

    .controls { margin-top: 16px; display: flex; flex-direction: column; gap: 12px; }
    .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
    button {
      background: #334155;
      color: var(--text);
      border: 1px solid #475569;
      padding: 9px 14px;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s, transform 0.05s;
    }
    button:hover { background: #475569; }
    button:active { transform: scale(0.97); }
    button.primary { background: #0284c7; border-color: #38bdf8; }
    button.primary:hover { background: #0369a1; }
    button.auto-active { background: #16a34a; border-color: #22c55e; }
    input[type="range"] { width: 100%; accent-color: var(--blue); cursor: pointer; }
    .footer-info {
      margin-top: 18px;
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
      <div class="conn-pill">
        <span class="dot" id="connDot"></span>
        <span id="connText">Connecting...</span>
      </div>
    </header>

    <div class="truck-banner" id="truckBanner">
      <div>
        <div class="truck-title" id="truckTitle">No Haul Truck Detected</div>
        <div class="truck-sub" id="truckSub">Monitoring NFC tag reader...</div>
      </div>
      <span class="badge" id="truckBadge">CLEAR</span>
    </div>

    <div class="grid">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Air Quality (PM Sensor)</span>
          <span class="badge" id="pmBadge">NORMAL</span>
        </div>
        <div class="metrics-row">
          <div class="metric">
            <div class="metric-label">PM2.5</div>
            <div class="metric-value" id="pm25">--</div>
            <div class="unit">&micro;g/m&sup3;</div>
          </div>
          <div class="metric">
            <div class="metric-label">PM10</div>
            <div class="metric-value" id="pm10">--</div>
            <div class="unit">&micro;g/m&sup3;</div>
          </div>
        </div>
        <div class="truck-sub" id="pmThresholdNote">Auto-boosts fan to 100% when PM &ge; 10 &micro;g/m&sup3;</div>
      </div>

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
      <button id="simNfcBtn" style="display:none; padding:4px 10px; font-size:0.8rem;" onclick="simNfc()">Simulate Haul Truck Tap</button>
    </div>
  </div>

  <script>
    const slider = document.getElementById("fanSlider");
    let sliderDragging = false;
    slider.addEventListener("pointerdown", () => sliderDragging = true);
    slider.addEventListener("pointerup", () => {
      sliderDragging = false;
      setFanDuty(parseInt(slider.value, 10));
    });
    slider.addEventListener("change", () => setFanDuty(parseInt(slider.value, 10)));

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

    async function poll() {
      try {
        const res = await fetch("/api/status");
        const d = await res.json();

        document.getElementById("connDot").classList.remove("offline");
        document.getElementById("connText").textContent = d.simulate ? "Connected (Simulation)" : "Connected (Live)";

        // Haul Truck / NFC
        const banner = document.getElementById("truckBanner");
        const truckTitle = document.getElementById("truckTitle");
        const truckSub = document.getElementById("truckSub");
        const truckBadge = document.getElementById("truckBadge");
        if (d.nfc_active) {
          banner.classList.add("detected");
          truckTitle.textContent = "HAUL TRUCK DETECTED";
          truckSub.textContent = "NFC Tag UID: " + (d.nfc_uid ? d.nfc_uid.join(" ") : "Detected");
          truckBadge.textContent = "ACTIVE";
          truckBadge.className = "badge alert";
        } else {
          banner.classList.remove("detected");
          truckTitle.textContent = "No Haul Truck Detected";
          truckSub.textContent = "Monitoring NFC tag reader...";
          truckBadge.textContent = "CLEAR";
          truckBadge.className = "badge";
        }

        // PM values
        const pm25El = document.getElementById("pm25");
        const pm10El = document.getElementById("pm10");
        pm25El.textContent = d.pm25 !== null ? d.pm25.toFixed(1) : "--";
        pm10El.textContent = d.pm10 !== null ? d.pm10.toFixed(1) : "--";
        pm25El.classList.toggle("high", d.pm25 !== null && d.pm25 >= d.pm_threshold);
        pm10El.classList.toggle("high", d.pm10 !== null && d.pm10 >= d.pm_threshold);

        const pmBadge = document.getElementById("pmBadge");
        pmBadge.textContent = d.pm_high ? "HIGH PM" : "NORMAL";
        pmBadge.className = d.pm_high ? "badge danger" : "badge";

        // Fan
        document.getElementById("fanDuty").textContent = d.fan_duty;
        document.getElementById("fanRpm").textContent = d.fan_rpm;
        document.getElementById("fanBar").style.width = d.fan_duty + "%";
        document.getElementById("reasonText").textContent = d.reason;
        if (!sliderDragging) slider.value = d.fan_duty;

        const modeBadge = document.getElementById("modeBadge");
        modeBadge.textContent = d.mode;
        modeBadge.className = d.mode === "MANUAL" ? "badge alert" : "badge";
        document.getElementById("autoBtn").className = d.mode === "AUTO" ? "auto-active" : "";

        document.getElementById("hwStatus").textContent =
          `Fan: ${d.hw_status.fan} | Air Sensor: ${d.hw_status.air} | NFC: ${d.hw_status.nfc}`;
        document.getElementById("simNfcBtn").style.display = d.simulate ? "inline-block" : "none";
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
            # Keep terminal output clean
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
            elif self.path == "/api/sim-nfc" and controller.simulate:
                controller.nfc.trigger()
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
    print(f"Dashboard running at:")
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
