import time
import lgpio
from rpi_hardware_pwm import HardwarePWM

TACH_PIN = 24
PULSES_PER_REV = 2
# Filter out any pulse/spike shorter than 2,000 microseconds (2 ms) at the driver level
DEBOUNCE_US = 2000

pulse_count = 0

def count_pulse(chip, gpio, level, timestamp):
    global pulse_count
    # level == 0 means falling edge
    if level == 0:
        pulse_count += 1

# Open gpiochip0 (standard on Raspberry Pi 3 B+)
h = lgpio.gpiochip_open(0)
lgpio.gpio_claim_alert(h, TACH_PIN, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
lgpio.gpio_set_debounce_micros(h, TACH_PIN, DEBOUNCE_US)
cb = lgpio.callback(h, TACH_PIN, lgpio.FALLING_EDGE, count_pulse)

pwm = HardwarePWM(pwm_channel=0, hz=25000, chip=0)
pwm.start(0)

def set_and_measure(duty):
    global pulse_count
    pwm.change_duty_cycle(duty)
    
    # Allow fan inertia to stabilize for 1.5 seconds
    time.sleep(5)
    
    pulse_count = 0
    start_time = time.monotonic()
    time.sleep(1.0)
    elapsed = time.monotonic() - start_time
    
    rpm = (pulse_count / PULSES_PER_REV) * (60.0 / elapsed)
    print(f"Duty Cycle: {duty:3d}% | Speed: {rpm:6.0f} RPM")

try:
    for duty in range(0, 101, 10):
        set_and_measure(duty)

    for duty in range(90, -1, -10):
        set_and_measure(duty)
finally:
    cb.cancel()
    lgpio.gpiochip_close(h)
    pwm.stop()
