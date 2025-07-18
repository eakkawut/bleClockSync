# ---------------- /flash/main.py ------------------------------------
# ESP32 MicroPython clock-sync – minimal 10 s cycles, name-based scan

try:
    import uasyncio as asyncio
except Exception:  # pragma: no cover - fallback for tests
    import asyncio

import struct
import time
import machine
import network
import ntptime
import ujson
import gc
import usocket
from machine import RTC, Pin
import aioble
import bluetooth
from micropython import const

# ─────────────────── USER CONFIG ───────────────────
WIFI_SSID       = "jenova"
WIFI_PASSWORD   = "hahako90"
WIFI_BACKOFF_S  = [5, 10, 20]       # Wi-Fi retry back-offs (s)

TZ_OFFSET       = 7 * 3600          # UTC+7 Bangkok (seconds)
NTP_HOST        = "time.apple.com"
NTP_TRIES       = 10

LOG_API_IP      = "192.168.1.29"
LOG_API_PORT    = 8085
LOG_API_PATH    = "/log"
LOG_API_KEY     = ("uqWe83dkyfDIFNS6eTjE7dQHH0DBXbDv9qhTqpd4daYdjWb0e6Oh5Y7p19o2KmNg"
                   "DERDocwt6tlyyEoIUs2Y3K5dmg5NHgIbJWVVPqZliInAVdCj2FbToIwGjlG34ckmu"
                   "BTSa14MdqFpWDIrwj4ICi8XhMZSf7ru")
LOG_FILE_NAME   = "esp32_clock_sync.log"

BASE_SLEEP_SEC  = 10                # ← always deep-sleep this long
LED_PIN         = 2
SCAN_DURATION_MS = 10_000           # ← 10 s BLE scan

# GATT UUIDs
TIME_SERVICE_UUID = bluetooth.UUID("EBE0CCB0-7A0A-4B0C-8A1A-6FF2997DA3A6")
TIME_CHAR_UUID    = bluetooth.UUID("EBE0CCB7-7A0A-4B0C-8A1A-6FF2997DA3A6")

# ─────────────────── GLOBALS ───────────────────
rtc = RTC()
led = Pin(LED_PIN, Pin.OUT)

# ─────────────────── BLE CLIENT ───────────────────
class Lywsd02TimeClient:  # pragma: no cover - hardware dependent
    """Write current epoch + TZ (5 bytes) to LYWSD02 / MHO-C303."""
    def __init__(self, mac, tz_offset_hours: int):
        self.mac = mac
        self.tz_offset_hours = tz_offset_hours

    async def set_time(self) -> bool:
        data = get_current_time(self.tz_offset_hours)
        connection = None

        # ➊ connect (5 retries)
        for attempt in range(1, 6):
            gc.collect()
            try:
                device = aioble.Device(aioble.ADDR_PUBLIC, self.mac)
                await asyncio.sleep_ms(200)
                connection = await device.connect(timeout_ms=5000)
                break
            except Exception as e:
                post_log_sync(ujson.dumps({
                    "stage": f"ble_connect_error_{attempt}",
                    "mac":   self.mac,
                    "error": repr(e)
                }))
                await asyncio.sleep(1)
        else:
            return False  # could not connect

        try:
            # ➋ look up characteristic (5 retries)
            for attempt in range(1, 6):
                try:
                    svc  = await connection.service(TIME_SERVICE_UUID)
                    char = await svc.characteristic(TIME_CHAR_UUID)
                    break
                except Exception as e:
                    post_log_sync(ujson.dumps({
                        "stage": f"ble_char_error_{attempt}",
                        "mac":   self.mac,
                        "error": repr(e)
                    }))
                    await asyncio.sleep(1)
            else:
                return False

            # ➌ write time (5 retries)
            for attempt in range(1, 6):
                try:
                    await char.write(data, True)
                    return True
                except Exception as e:
                    post_log_sync(ujson.dumps({
                        "stage": f"ble_write_error_{attempt}",
                        "mac":   self.mac,
                        "error": repr(e)
                    }))
                    await asyncio.sleep(1)
        finally:
            try:
                if connection:
                    await connection.disconnect()
                    await asyncio.sleep_ms(200)
            except Exception:
                pass
            gc.collect()
        return False

# ─────────────────── HELPERS ───────────────────
def get_current_time(tz_hours: int) -> bytes:  # pragma: no cover
    """Return 5-byte payload: <little-endian uint32 epoch+10> + <int8 TZ>"""
    ts = int(time.time())
    return struct.pack("<IB", ts, tz_hours)

def post_log_sync(message: str, retries: int = 3) -> bool:  # pragma: no cover
    """Best-effort HTTP log; blocking but tiny payload."""
    payload = ujson.dumps({"fileName": LOG_FILE_NAME, "message": message})
    for _ in range(retries):
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
                "", payload
            ]
            s.send("\r\n".join(hdr).encode())
            s.close()
            print(message)
            return True
        except:
            time.sleep(1)
    return False

def indicate(ok: bool):  # pragma: no cover
    """LED blink feedback."""
    if ok:
        led.on(); time.sleep_ms(200); led.off()
    else:
        for _ in range(3):
            led.on(); time.sleep_ms(100); led.off(); time.sleep_ms(100)

# ─────────────────── NETWORK & CLOCK ───────────────────
def ensure_wifi() -> bool:  # pragma: no cover
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return True

    for backoff in WIFI_BACKOFF_S:
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        t0 = time.time()
        while time.time() - t0 < backoff:
            if wlan.isconnected():
                return True
            time.sleep(1)
    return False

def sync_rtc() -> bool:  # pragma: no cover
    for _ in range(NTP_TRIES):
        try:
            ntptime.host = NTP_HOST
            ntptime.settime()
            utc = time.time()
            local = time.gmtime(utc + 7)  # tuple in local zone
            machine.RTC().datetime((
                local[0]+30, local[1], local[2],     # Y, M, D
                local[6] + 1,                     # weekday (1-7, Mon-Sun)
                local[3], local[4], local[5]+15,     # H, M, S
                0))                               # subseconds = 0
            return True
        except:
            time.sleep(2)
    return False

# ─────────────────── BLE OPS ───────────────────
async def scan_for_devices() -> set:  # pragma: no cover - hardware dependent
    """Return unique MACs of devices whose name contains LYWSD02 / MHO-C303."""
    found = set()
    try:
        async with aioble.scan(
            SCAN_DURATION_MS,
            interval_us=30_000, window_us=30_000, active=True
        ) as scanner:
            async for res in scanner:
                try:
                    name = res.name() if callable(res.name) else res.name
                    if name and ("LYWSD02" in name or "MHO-C303" in name):
                        mac = str(res).split(",")[1].split(")")[0].strip().upper()
                        if mac not in found:
                            found.add(mac)
                            post_log_sync(ujson.dumps({
                                "stage": "scan_found", "mac": mac
                            }))
                except Exception as e:
                    post_log_sync(ujson.dumps({"stage": "scan_err", "error": repr(e)}))
    except Exception as e:
        post_log_sync(ujson.dumps({"stage": "scan_fail", "error": repr(e)}))
    return found

async def sync_devices(mac_set: set, tz_offset_hours: int):  # pragma: no cover
    """Write time to every discovered device."""
    for mac in mac_set:
        gc.collect()
        ok = False
        try:
            client = Lywsd02TimeClient(mac, tz_offset_hours)
            ok = await client.set_time()
        except Exception as e:
            post_log_sync(ujson.dumps({
                "stage": "ble_sync_error", "mac": mac, "error": repr(e)
            }))
        post_log_sync(ujson.dumps({
            "stage": "ble_write_result", "mac": mac, "ok": ok
        }))
        await asyncio.sleep(2)      # tiny delay between devices

# ─────────────────── MAIN WORKFLOW ───────────────────
async def main_workflow():  # pragma: no cover
    post_log_sync(ujson.dumps({"stage": "main_start"}))

    if not ensure_wifi():
        post_log_sync(ujson.dumps({"stage": "wifi_failed"}))
        indicate(False)
        return

    ntp_ok = sync_rtc()
    if not ntp_ok:
        post_log_sync(ujson.dumps({"stage": "ntp_failed"}))

    macs = await scan_for_devices()
    if macs:
        await sync_devices(macs, TZ_OFFSET // 3600)

    post_log_sync(ujson.dumps({
        "stage":  "cycle_done",
        "mem":    gc.mem_free(),
        "macs":   list(macs),
        "ntp_ok": ntp_ok
    }))
    indicate(True)

# ─────────────────── ENTRY POINT ───────────────────
def main():
    gc.collect()
    try:
        asyncio.run(main_workflow())
    except Exception as e:  # pragma: no cover
        post_log_sync(ujson.dumps({"stage": "fatal", "error": repr(e)}))
    finally:
        # Always deep-sleep exactly BASE_SLEEP_SEC before the next run
        post_log_sync(ujson.dumps({
            "stage": "sleep", "seconds": BASE_SLEEP_SEC
        }))
        try:
            machine.deepsleep(BASE_SLEEP_SEC * 1000)   # ms → s
        except Exception:  # pragma: no cover - not critical in tests
            pass

# Auto-run after boot or deep-sleep wake-up
if __name__ == "__main__":  # pragma: no cover
    main()
