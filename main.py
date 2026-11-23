import os
import sys
import time
import threading
import serial
import lgpio
from rpi_hardware_pwm import HardwarePWM

# Allow importing the local Waveshare PN532 driver
PN532_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Pn532-nfc-hat-code",
    "raspberrypi",
    "python",
)
if PN532_DIR not in sys.path:
    sys.path.insert(0, PN532_DIR)

from pn532 import PN532_SPI

# --- Configuration ---
DEFAULT_DUTY = 20          # Default fan speed (%)
HIGH_DUTY = 100            # Boosted fan speed (%)
PM_THRESHOLD = 10.0        # PM2.5 or PM10 threshold (µg/m³) to trigger 100%
NFC_HOLD_SECONDS = 10.0    # Keep fan at 100% for this many seconds after an NFC tap

SERIAL_PORT = "/dev/ttyUSB0"
SERIAL_BAUD = 9600

TACH_PIN = 24
PULSES_PER_REV = 2
DEBOUNCE_US = 2000
PWM_CHANNEL = 0
PWM_FREQ_HZ = 25000
PWM_CHIP = 0

PN532_RESET_PIN = 20
PN532_CS_PIN = 4


class FanController:
    def __init__(self, initial_duty=DEFAULT_DUTY):
        self._lock = threading.Lock()
        self._pulse_count = 0
        self._last_rpm_time = time.monotonic()
        self._current_duty = None

        self._h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_alert(self._h, TACH_PIN, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
        lgpio.gpio_set_debounce_micros(self._h, TACH_PIN, DEBOUNCE_US)
        self._cb = lgpio.callback(self._h, TACH_PIN, lgpio.FALLING_EDGE, self._count_pulse)

        self._pwm = HardwarePWM(pwm_channel=PWM_CHANNEL, hz=PWM_FREQ_HZ, chip=PWM_CHIP)
        self._pwm.start(initial_duty)
        self._current_duty = initial_duty

    def _count_pulse(self, chip, gpio, level, timestamp):
        if level == 0:
            with self._lock:
                self._pulse_count += 1

    def set_duty(self, duty):
        if duty != self._current_duty:
            self._pwm.change_duty_cycle(duty)
            self._current_duty = duty

    @property
    def duty(self):
        return self._current_duty

    def get_rpm(self):
        now = time.monotonic()
        with self._lock:
            pulses = self._pulse_count
            self._pulse_count = 0
        elapsed = now - self._last_rpm_time
        self._last_rpm_time = now
        if elapsed <= 0:
            return 0.0
        return (pulses / PULSES_PER_REV) * (60.0 / elapsed)

    def cleanup(self):
        self._cb.cancel()
        lgpio.gpiochip_close(self._h)
        self._pwm.stop()


class AirQualitySensor:
    """Background reader for SDS011 PM2.5 / PM10 sensor over USB serial."""

    def __init__(self, port=SERIAL_PORT, baud=SERIAL_BAUD):
        self._port = port
        self._baud = baud
        self._lock = threading.Lock()
        self._pm25 = None
        self._pm10 = None
        self._stop_event = threading.Event()
        self._ser = serial.Serial(self._port, self._baud, timeout=2)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        self._ser.reset_input_buffer()
        while not self._stop_event.is_set():
            try:
                # Sync to SDS011 frame header (0xAA 0xC0)
                b1 = self._ser.read(1)
                if b1 != b"\xaa":
                    continue
                b2 = self._ser.read(1)
                if b2 != b"\xc0":
                    continue

                payload = self._ser.read(8)
                if len(payload) != 8 or payload[7] != 0xAB:
                    continue

                # Verify checksum (sum of data bytes 0..5 mod 256 == byte 6)
                if (sum(payload[0:6]) & 0xFF) != payload[6]:
                    continue

                pm25 = int.from_bytes(payload[0:2], byteorder="little") / 10.0
                pm10 = int.from_bytes(payload[2:4], byteorder="little") / 10.0
                with self._lock:
                    self._pm25 = pm25
                    self._pm10 = pm10
            except serial.SerialException as e:
                print(f"[AirQuality] Serial error: {e}")
                time.sleep(1)

    def get_readings(self):
        with self._lock:
            return self._pm25, self._pm10

    def stop(self):
        self._stop_event.set()
        if self._ser.is_open:
            self._ser.close()


class NFCMonitor:
    """Background reader for Waveshare PN532 NFC HAT over SPI."""

    def __init__(self, reset=PN532_RESET_PIN, cs=PN532_CS_PIN, hold_seconds=NFC_HOLD_SECONDS):
        self._hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self._last_detected = 0.0
        self._last_uid = None
        self._stop_event = threading.Event()

        self._pn532 = PN532_SPI(debug=False, reset=reset, cs=cs)
        ic, ver, rev, support = self._pn532.get_firmware_version()
        print(f"[NFC] Found PN532 firmware {ver}.{rev}")
        self._pn532.SAM_configuration()

        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                uid = self._pn532.read_passive_target(timeout=0.5)
                if uid is not None:
                    uid_hex = [hex(i) for i in uid]
                    with self._lock:
                        self._last_detected = time.monotonic()
                        self._last_uid = uid_hex
                    print(f"[NFC] Tag detected! UID: {uid_hex}")
            except Exception as e:
                print(f"[NFC] Read error: {e}")
                time.sleep(0.5)

    def is_active(self):
        with self._lock:
            if self._last_detected == 0.0:
                return False, None
            active = (time.monotonic() - self._last_detected) <= self._hold_seconds
            return active, self._last_uid

    def stop(self):
        self._stop_event.set()


def main():
    print("Starting UBCProject integrated controller...")
    fan = FanController(initial_duty=DEFAULT_DUTY)
    air_sensor = AirQualitySensor()
    nfc = NFCMonitor()

    air_sensor.start()
    nfc.start()

    print(
        f"Running: Fan default={DEFAULT_DUTY}%, boost={HIGH_DUTY}% "
        f"(PM >= {PM_THRESHOLD} or NFC detected, hold={NFC_HOLD_SECONDS}s). "
        "Press Ctrl+C to stop."
    )

    try:
        while True:
            time.sleep(1.0)
            pm25, pm10 = air_sensor.get_readings()
            nfc_active, uid = nfc.is_active()

            pm_high = (
                (pm25 is not None and pm25 >= PM_THRESHOLD)
                or (pm10 is not None and pm10 >= PM_THRESHOLD)
            )

            reasons = []
            if nfc_active:
                reasons.append("NFC")
            if pm_high:
                reasons.append(f"PM>={PM_THRESHOLD}")

            target_duty = HIGH_DUTY if reasons else DEFAULT_DUTY
            fan.set_duty(target_duty)
            rpm = fan.get_rpm()

            pm25_str = f"{pm25:.1f}" if pm25 is not None else "--"
            pm10_str = f"{pm10:.1f}" if pm10 is not None else "--"
            reason_str = "+".join(reasons) if reasons else "DEFAULT"

            print(
                f"PM2.5: {pm25_str:>5} | PM10: {pm10_str:>5} | "
                f"NFC: {'YES' if nfc_active else 'NO ':3} | "
                f"Fan: {fan.duty:3d}% ({rpm:5.0f} RPM) [{reason_str}]"
            )
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        air_sensor.stop()
        nfc.stop()
        fan.cleanup()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
