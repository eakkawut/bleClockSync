import uasyncio as asyncio
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
from micropython import const

# ===================== USER CONFIG =====================
WIFI_SSID = "jenova"
WIFI_PASSWORD = "hahako90"
WIFI_BACKOFF_S = [5, 10, 20]

TZ_OFFSET = 25230  # UTC+7 Bangkok (seconds)
NTP_HOST = "time.apple.com"
NTP_TRIES = 5

LY_CLOCKS = [
    "18:5E:D1:40:DC:CB",
    "E7:2E:00:50:60:74",
]

LOG_API_IP = "192.168.1.29"
LOG_API_PORT = 8085
LOG_API_PATH = "/log"
LOG_API_KEY = "uqWe83dkyfDIFNS6eTjE7dQHH0DBXbDv9qhTqpd4daYdjWb0e6Oh5Y7p19o2KmNgDERDocwt6tlyyEoIUs2Y3K5dmg5NHgIbJWVVPqZliInAVdCj2FbToIwGjlG34ckmuBTSa14MdqFpWDIrwj4ICi8XhMZSf7ru"
LOG_FILE_NAME = "esp32_clock_sync.log"

BASE_SLEEP_SEC = 4 * 3600  # 4 hours
LED_PIN = 2
SCAN_DURATION_MS = 15 * 60 * 1000  # 15 minutes

# GATT UUIDs
TIME_SERVICE_UUID = const("EBE0CCB0-7A0A-4B0C-8A1A-6FF2997DA3A6")
TIME_CHAR_UUID = const("EBE0CCB7-7A0A-4B0C-8A1A-6FF2997DA3A6")

# ===================== GLOBAL OBJECTS =====================
rtc = RTC()
led = Pin(LED_PIN, Pin.OUT)

# ===================== LYWSD02 CLIENT =====================
class Lywsd02TimeClient:
    def __init__(self, mac, tz_offset_hours):
        self.mac = mac
        self.tz_offset_hours = tz_offset_hours
    
    async def set_time(self, timestamp_utc=None):
        """Set device time using UTC timestamp and timezone offset"""
        if timestamp_utc is None:
            timestamp_utc = time.time()
        
        # Pack data: 4-byte timestamp + 1-byte timezone
        data = struct.pack('<Ib', int(timestamp_utc), self.tz_offset_hours)
        
        try:
            # Connect to device
            device = aioble.Device(aioble.ADDR_RANDOM, self.mac)
            connection = await device.connect(timeout_ms=10000)
            
            # Access time service and characteristic
            service = await connection.service(TIME_SERVICE_UUID)
            char = await service.characteristic(TIME_CHAR_UUID)
            
            # Write time data with response
            await char.write(data, True)
            return True
        except (asyncio.TimeoutError, OSError) as e:
            return False
        finally:
            if 'connection' in locals():
                await connection.disconnect()

# ===================== UTILITY FUNCTIONS =====================
def post_log_sync(message: str, retries: int = 3) -> bool:
    """Send log message to API"""
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
                "",
                payload
            ]
            s.send("\r\n".join(hdr).encode())
            s.close()
            return True
        except:
            time.sleep(1)
    return False

def indicate(success: bool):
    """Visual feedback using LED"""
    if success:
        led.on(); time.sleep_ms(200); led.off()
    else:
        for _ in range(3):
            led.on(); time.sleep_ms(100); led.off(); time.sleep_ms(100)

def load_state() -> dict:
    """Load state from RTC memory"""
    try:
        mem = rtc.memory()
        return ujson.loads(mem) if mem else {}
    except:
        return {}

def save_state(st: dict):
    """Save state to RTC memory"""
    rtc.memory(ujson.dumps(st))

# ===================== NETWORK & TIME FUNCTIONS =====================
def ensure_wifi() -> bool:
    """Connect to WiFi with retry logic"""
    post_log_sync(ujson.dumps({"stage": "ensure_wifi_start"}))
    state = load_state()
    fails = state.get("wifi_fails", 0)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    for backoff in WIFI_BACKOFF_S:
        if wlan.isconnected():
            post_log_sync(ujson.dumps({"stage": "wifi_already_connected"}))
            state["wifi_fails"] = 0
            save_state(state)
            return True
            
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        t0 = time.time()
        while time.time() - t0 < backoff:
            if wlan.isconnected():
                post_log_sync(ujson.dumps({
                    "stage": "wifi_connected", 
                    "attempt_backoff": backoff
                }))
                state["wifi_fails"] = 0
                save_state(state)
                return True
            time.sleep(1)
        fails += 1
        
    state["wifi_fails"] = fails
    save_state(state)
    post_log_sync(ujson.dumps({
        "stage": "ensure_wifi_failed", 
        "fails": fails
    }))
    return False

def sync_rtc() -> bool:
    """Sync ESP32 RTC with NTP server"""
    post_log_sync(ujson.dumps({"stage": "sync_rtc_start"}))
    state = load_state()
    
    for attempt in range(1, NTP_TRIES + 1):
        try:
            ntptime.host = NTP_HOST
            ntptime.settime()
            utc = time.time()
            
            # Validate time (year should be >= 2023)
            if time.gmtime(utc)[0] < 2023:
                raise ValueError("Invalid time received from NTP")
                
            state["ntp_fails"] = 0
            save_state(state)
            post_log_sync(ujson.dumps({"stage": "sync_rtc_success"}))
            return True
        except Exception as e:
            state["ntp_fails"] = state.get("ntp_fails", 0) + 1
            save_state(state)
            post_log_sync(ujson.dumps({
                "stage": "sync_rtc_error", 
                "try": attempt, 
                "error": str(e)
            }))
            time.sleep(2)
    return False

# ===================== BLE OPERATIONS =====================
async def scan_for_devices():
    """Scan for LYWSD02 devices, stopping early if all targets found"""
    # Create uppercase set of target MACs for case-insensitive matching
    target_macs = {mac.upper() for mac in LY_CLOCKS}
    found_targets = set()
    
    post_log_sync(ujson.dumps({
        "stage": "scan_start", 
        "duration_ms": SCAN_DURATION_MS,
        "targets": list(target_macs)
    }))
    
    try:
        # Start scanning with optimized parameters
        async with aioble.scan(
            SCAN_DURATION_MS, 
            interval_us=30000, 
            window_us=30000, 
            active=True
        ) as scanner:
            # Track time to detect when we've found all targets
            start_time = time.ticks_ms()
            
            async for result in scanner:
                # Check if we've found all targets
                if found_targets.issuperset(target_macs):
                    post_log_sync(ujson.dumps({
                        "stage": "scan_complete_early", 
                        "found_all": True,
                        "elapsed_ms": time.ticks_diff(time.ticks_ms(), start_time)
                    }))
                    break
                
                try:
                    # Extract MAC address from advertisement
                    mac = str(result).split(',')[1].split(')')[0].strip().upper()
                    
                    # Extract device name safely
                    name = result.name() if callable(result.name) else result.name
                    
                    # Check if device matches our targets
                    if mac in target_macs or (name and ("LYWSD02" in name or "MHO-C303" in name)):
                        found_targets.add(mac)
                        post_log_sync(ujson.dumps({
                            "stage": "scan_found", 
                            "mac": mac,
                            "remaining": len(target_macs - found_targets)
                        }))
                        
                        # Stop scanning immediately if we have all targets
                        if found_targets.issuperset(target_macs):
                            post_log_sync(ujson.dumps({
                                "stage": "scan_complete_early", 
                                "found_all": True,
                                "elapsed_ms": time.ticks_diff(time.ticks_ms(), start_time)
                            }))
                            break
                except Exception as e:
                    post_log_sync(ujson.dumps({
                        "stage": "scan_error",
                        "error": str(e)
                    }))
    except Exception as e:
        post_log_sync(ujson.dumps({
            "stage": "scan_failed",
            "error": str(e)
        }))
    
    return found_targets

async def sync_devices(devices, tz_offset_hours):
    """Sync time with found devices"""
    results = {}
    for mac in devices:
        gc.collect()
        try:
            client = Lywsd02TimeClient(mac, tz_offset_hours)
            success = await client.set_time()
            results[mac] = success
            
            post_log_sync(ujson.dumps({
                "stage": "ble_write_result", 
                "mac": mac, 
                "ok": success
            }))
            
            # Delay between device syncs
            await asyncio.sleep(3)
        except Exception as e:
            results[mac] = False
            post_log_sync(ujson.dumps({
                "stage": "ble_sync_error",
                "mac": mac,
                "error": str(e)
            }))
    return results

# ===================== MAIN WORKFLOW =====================
async def main_workflow():
    """Main async workflow"""
    # Load state and determine sleep duration
    state = load_state()
    ntp_fails = state.get("ntp_fails", 0)
    sleep_sec = BASE_SLEEP_SEC * (4 if ntp_fails >= 6 else 2 if ntp_fails >= 3 else 1)
    
    # 1. Ensure WiFi connection
    post_log_sync(ujson.dumps({"stage": "main_start"}))
    if not ensure_wifi():
        post_log_sync(ujson.dumps({"stage": "main_wifi_failed"}))
        indicate(False)
        return sleep_sec

    # 2. Sync RTC with NTP
    ok_ntp = sync_rtc()
    
    # 3. Scan and sync devices
    found_devices = await scan_for_devices()
    if found_devices:
        # Convert timezone offset to hours
        tz_hours = TZ_OFFSET // 3600
        await sync_devices(found_devices, tz_hours)
    
    # 4. Log results and prepare for sleep
    summary = ujson.dumps({
        "ntp_ok": ok_ntp,
        "ntp_fails": state.get("ntp_fails", 0),
        "mem_free": gc.mem_free(),
        "sleep_s": sleep_sec
    })
    post_log_sync(ujson.dumps({
        "stage": "main_summary", 
        "summary": summary
    }))
    
    indicate(True)
    return sleep_sec

def main():
    """Entry point with error handling"""
    gc.collect()
    try:
        # Run main workflow
        sleep_sec = asyncio.run(main_workflow())
        
        # Prepare for deep sleep
        post_log_sync(ujson.dumps({
            "stage": "main_sleeping", 
            "sleep_sec": sleep_sec
        }))
        machine.deepsleep(sleep_sec * 1000)
        
    except Exception as ex:
        # Critical error handling
        post_log_sync(ujson.dumps({
            "stage": "fatal", 
            "error": str(ex)
        }))
        time.sleep(5)
        machine.reset()

if __name__ == "__main__":
    main()