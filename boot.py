"""Boot utilities for Wi-Fi and RTC setup."""

import time

import network
import ntptime
import machine

try:  # check optional dependency available for tests
    import uasyncio as asyncio
except Exception:  # pragma: no cover - fallback for tests
    asyncio = None

SSID = "jenova"
PASSWORD = "hahako90"


def ensure_dependencies():
    """Import all required modules, installing with upip if missing."""
    modules = [
        "uasyncio",
        "struct",
        "time",
        "machine",
        "network",
        "ntptime",
        "ujson",
        "gc",
        "usocket",
        "aioble",
        "bluetooth",
    ]
    for mod in modules:
        try:
            __import__(mod)
        except Exception:  # pragma: no cover
            try:
                import upip
                upip.install(mod)
            except Exception as e:  # pragma: no cover - best effort
                print("could not install", mod, e)
    # check submodules
    try:
        from machine import RTC, Pin  # noqa: F401
    except Exception:  # pragma: no cover
        pass  # pragma: no cover
    try:
        from micropython import const  # noqa: F401
    except Exception:  # pragma: no cover
        try:
            import upip
            upip.install("micropython")
        except Exception as e:  # pragma: no cover
            print("could not install micropython", e)


def connect_wifi(
    ssid: str = SSID,
    password: str = PASSWORD,
    attempts: int = 10,
    check_seconds: int = 3,
    max_internet_tries: int = 3,
    deep_sleep_ms: int = 60_000,
) -> bool:
    """Connect to Wi-Fi and verify internet access.

    If Wi-Fi or internet access cannot be established after all
    attempts, the device deep sleeps for ``deep_sleep_ms`` milliseconds.
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    for _ in range(attempts):
        if not wlan.isconnected():
            try:
                wlan.connect(ssid, password)
            except Exception as e:  # pragma: no cover - best effort
                print("wifi connect error", e)
        t0 = time.time()
        while time.time() - t0 < check_seconds:
            if wlan.isconnected():
                break
            time.sleep(1)  # pragma: no cover

        if wlan.isconnected():
            for _ in range(max_internet_tries):
                try:
                    ntptime.settime()  # check internet connectivity
                    print("Wi-Fi:", wlan.ifconfig())
                    return True
                except Exception as e:  # pragma: no cover - best effort
                    print("internet check error", e)
                    time.sleep(1)

    print("Wi-Fi or internet not available, deep sleeping")  # pragma: no cover
    try:
        machine.deepsleep(deep_sleep_ms)
    except Exception:  # pragma: no cover - not critical in tests
        pass
    return False


def sync_rtc(tries: int = 5, tz_offset: int = 25205) -> bool:
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
        except Exception as err:  # pragma: no cover
            print(f"NTP try {attempt}/{tries} failed →", err)
            time.sleep(2)                         # wait 2 s before retry
    return False  # pragma: no cover


def main() -> None:
    """Entry point executed on boot."""
    try:
        connect_wifi()
        ensure_dependencies()
        sync_rtc()
    except Exception as err:  # pragma: no cover - safety net
        print("boot error", err)


if __name__ == "__main__":  # pragma: no cover
    main()
