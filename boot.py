import network, time, ntptime, machine

SSID, PASSWORD = "jenova", "hahako90"

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
if not wlan.isconnected():
    wlan.connect(SSID, PASSWORD)
    for _ in range(15):
        if wlan.isconnected():
            break
        time.sleep(1)

print("Wi-Fi:", wlan.ifconfig())


def sync_rtc(tries=5, tz_offset=25205):
    ntptime.host = "time.apple.com"
    for attempt in range(1, tries + 1):
        try:
            ntptime.settime()                     # RTC ← UTC from time.apple.com
            # shift RTC to local time (optional – remove if you prefer UTC)
            utc = time.time()
            local = time.gmtime(utc + tz_offset)  # tuple in local zone
            machine.RTC().datetime((
                local[0], local[1], local[2],     # Y, M, D
                local[6] + 1,                     # weekday (1-7, Mon-Sun)
                local[3], local[4], local[5],     # H, M, S
                0))                               # subseconds = 0
            print(f"RTC set via {ntptime.host} on try {attempt}:",
                  time.localtime())
            return True
        except Exception as err:
            print(f"NTP try {attempt}/{tries} failed →", err)
            time.sleep(2)                         # wait 2 s before retry
    return False

sync_rtc()
