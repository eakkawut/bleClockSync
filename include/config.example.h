// Copy to config.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID          "your-ssid"
#define WIFI_PASSWORD      "your-password"
#define WIFI_TIMEOUT_MS    20000

#define NTP_HOST           "time.apple.com"
#define NTP_TRIES          5          // x 5 s each

// Local timezone: POSIX string for the midnight schedule, plain hours for the clocks
#define LOCAL_TZ           "ICT-7"
#define TZ_HOURS           7

// Seconds added to the NTP time at the moment it is written to a clock
#define CLOCK_LEAD_SEC     40

#define CLOCK_MACS         { "00:00:00:00:00:00", "00:00:00:00:00:01" }
#define CONNECT_TIMEOUT_MS 10000
#define RETRY_DELAY_S      5
#define GIVE_UP_AFTER_S    (6 * 3600)

#define WAKE_WINDOW_S      120
#define NTP_RETRY_S        3600
