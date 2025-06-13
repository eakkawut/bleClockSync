# ---------------- /flash/main.py ------------------------------------
# ESP32 MicroPython clock sync

import gc
import time
import uos
import ujson
import usocket
import _thread
import ntptime
import struct
import machine
import network
from machine import RTC, WDT, Pin
from bluetooth import BLE

# ---------------- ❶ USER CONFIG ----------------
WIFI_SSID      = "jenova"
WIFI_PASSWORD  = "hahako90"
WIFI_BACKOFF_S = [5, 10, 20]

TZ_OFFSET   = 7 * 3600            # UTC+7
NTP_HOST    = "time.apple.com"
NTP_TRIES   = 5

LY_CLOCKS = [
    "A4:C1:38:12:34:56",
    "A4:C1:38:65:43:21",
]

LOG_API_IP     = "192.168.1.29"
LOG_API_PORT   = 8085
LOG_API_PATH   = "/log"
LOG_API_KEY    = "hahako90pfx58tOdCikxdpFd9PW9S8RFPqejxFbX"
LOG_CACHE_FILE = "log_cache.txt"

BASE_SLEEP_SEC = 4 * 3600         # 4 h
WDT_TIMEOUT_MS = 15_000           # 15 s

LED_PIN       = 2
DIAG_PORT     = 9090
DIAG_TIMEOUT  = 30

# ---------------- Globals ----------------
boot_time = time.time()
rtc       = RTC()
wdt       = WDT(timeout=WDT_TIMEOUT_MS)
led       = Pin(LED_PIN, Pin.OUT)

# ---------------- Utility Functions ----------------
def cache_log(entry):
    try:
        with open(LOG_CACHE_FILE, "a") as f:
            f.write(ujson.dumps(entry) + "\n")
    except:
        pass

# Blocking POST but non-blocking caller; feeds watchdog inside loop
def post_log_sync(entry, retries=3):
    payload = ujson.dumps(entry)
    for _ in range(retries):
        wdt.feed()
        try:
            s = usocket.socket()
            s.settimeout(2)
            s.connect((LOG_API_IP, LOG_API_PORT))
            hdr = [
                f"POST {LOG_API_PATH} HTTP/1.1",
                f"Host: {LOG_API_IP}",
                f"X-API-Key: {LOG_API_KEY}",
                "Content-Type: application/json",
                f"Content-Length: {len(payload)}",
                "",
                payload
            ]
            s.send("\r\n".join(hdr).encode())
            s.close()
            return True
        except:
            time.sleep(1)
    return False

# Flush all cached logs (blocking)
def flush_log_cache():
    if LOG_CACHE_FILE in uos.listdir():
        try:
            lines = open(LOG_CACHE_FILE).read().splitlines()
            for ln in lines:
                entry = ujson.loads(ln)
                if not post_log_sync(entry):
                    return False
            uos.remove(LOG_CACHE_FILE)
        except:
            return False
    return True

def indicate(success: bool):
    if success:
        led.on(); time.sleep_ms(200); led.off()
    else:
        for _ in range(3):
            led.on(); time.sleep_ms(100); led.off(); time.sleep_ms(100)

# ---------------- Adaptive Sleep ----------------
def load_state():
    try:
        mem = rtc.memory()
        return ujson.loads(mem) if mem else {}
    except:
        return {}

def save_state(st: dict):
    rtc.memory(ujson.dumps(st))

state = load_state()
state.setdefault("ntp_fails", 0)
if "diag_token" not in state:
    import ubinascii, os
    state["diag_token"] = ubinascii.hexlify(os.urandom(4)).decode()
token = state["diag_token"]

sf = state["ntp_fails"]
if sf >= 6:
    sleep_sec = BASE_SLEEP_SEC * 4
elif sf >= 3:
    sleep_sec = BASE_SLEEP_SEC * 2
else:
    sleep_sec = BASE_SLEEP_SEC

# ---------------- Diagnostic Server ----------------
def diag_server(token):
    s = usocket.socket()
    s.settimeout(DIAG_TIMEOUT)
    try:
        s.bind(("0.0.0.0", DIAG_PORT))
        s.listen(1)
        end = time.time() + DIAG_TIMEOUT
        while time.time() < end:
            try:
                cl, _ = s.accept()
                req = cl.readline().decode()
                if f"/diag?token={token}" in req:
                    info = {
                        "mem_free": gc.mem_free(),
                        "uptime_s": time.time() - boot_time,
                        "sleep_s": sleep_sec,
                        "ntp_fails": state["ntp_fails"],
                        "cache_len": len(open(LOG_CACHE_FILE).read().splitlines())
                                     if LOG_CACHE_FILE in uos.listdir() else 0
                    }
                    body = ujson.dumps(info)
                    hdr = "\r\n".join([
                        "HTTP/1.1 200 OK",
                        "Content-Type: application/json",
                        f"Content-Length: {len(body)}",
                        "", body
                    ])
                    cl.send(hdr.encode())
                else:
                    cl.send(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                cl.close()
            except:
                pass
    finally:
        try: s.close()
        except: pass

# ---------------- Network & NTP ----------------
def ensure_wifi():
    fails = state.get("wifi_fails", 0)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    for backoff in WIFI_BACKOFF_S:
        wdt.feed()
        if wlan.isconnected():
            state["wifi_fails"] = 0
            save_state(state)
            return True
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        t0 = time.time()
        while time.time() - t0 < backoff:
            if wlan.isconnected():
                state["wifi_fails"] = 0
                save_state(state)
                return True
            time.sleep(1)
        fails += 1
    state["wifi_fails"] = fails
    save_state(state)
    return False

def sync_rtc():
    for attempt in range(1, NTP_TRIES + 1):
        wdt.feed()
        try:
            ntptime.host = NTP_HOST
            ntptime.settime()
            utc = time.time()
            lm = time.gmtime(utc + TZ_OFFSET)
            if lm[0] < 2025:
                raise Exception("Bad year from NTP")
            dow = lm[6] + 1 if lm[6] else 7
            rtc.datetime((lm[0], lm[1], lm[2], dow,
                          lm[3], lm[4], lm[5], 0))
            state["ntp_fails"] = 0
            save_state(state)
            return True
        except Exception as e:
            state["ntp_fails"] += 1
            save_state(state)
            cache_log({"stage": "ntp", "try": attempt, "error": str(e)})
            time.sleep(2)
    return False

# ---------------- BLE Time Sync ----------------
def _exact_time_packet():
    tm = time.localtime()
    dow = tm[6] + 1 if tm[6] else 7
    # little-endian: year(uint16), month, day, hour, minute, second, weekday, flags
    return struct.pack('<HBBBBBBH', tm[0], tm[1], tm[2], tm[3], tm[4], tm[5], dow, 0x0100)

def sync_lywsd(mac, ble):
    start = time.ticks_ms()
    addr = bytes(int(b,16) for b in mac.split(":")[::-1])
    try:
        if not ble.gap_connect(0x00, addr):
            return False
        while time.ticks_diff(time.ticks_ms(), start) < 1000:
            wdt.feed()
            ev = ble.events()
            if ev and ev[0] == 1:
                conn = ev[2]
                break
            time.sleep_ms(20)
        else:
            return False
        svcs  = ble.gattc_services(conn)
        chars = ble.gattc_characteristics(conn, svcs[0][0])
        for c in chars:
            if c[2].lower() == "2a2b":
                ble.gattc_write(conn, c[0], _exact_time_packet(), 1)
                ble.gap_disconnect(conn)
                return True
        ble.gap_disconnect(conn)
        return False
    except Exception as e:
        cache_log({"stage": "ble", "mac": mac, "error": str(e)})
        try: ble.gap_disconnect(conn)
        except: pass
        return False

# ---------------- Main ----------------
def main():
    try:
        time.sleep_ms(200)
        if not ensure_wifi():
            cache_log({"stage": "wifi_fail"})
            indicate(False)
            machine.deepsleep(sleep_sec * 1000)

        # start diagnostics listener
        _thread.start_new_thread(diag_server, (token,))

        # flush old logs
        flush_log_cache()

        ok_ntp = sync_rtc()
        ble = BLE(); ble.active(True)
        results = []
        for mac in LY_CLOCKS:
            wdt.feed()
            ok = sync_lywsd(mac, ble)
            results.append({"mac": mac, "ok": ok})

        entry = {
            "timestamp": time.time(),
            "ntp_ok": ok_ntp,
            "ntp_fails": state["ntp_fails"],
            "mem_free": gc.mem_free(),
            "sleep_s": sleep_sec,
            "clocks": results,
            "diag_token": token
        }

        # log asynchronously
        _thread.start_new_thread(
            lambda e: (flush_log_cache() or True) and (post_log_sync(e) or cache_log(e)),
            (entry,)
        )
        indicate(True)
        ble.active(False)
        machine.deepsleep(sleep_sec * 1000)

    except Exception as ex:
        import uio, sys
        buf = uio.StringIO()
        sys.print_exception(ex, buf)
        cache_log({"stage": "fatal", "trace": buf.getvalue()})
        time.sleep(5)
        machine.reset()

if __name__ == "__main__":
    main()
# --------------------------------------------------------------------
