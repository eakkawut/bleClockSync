// bleClockSync: every local midnight, fetch NTP time over Wi-Fi, then write it
// (+CLOCK_LEAD_SEC) to each LYWSD02 / MHO-C303 clock over BLE, retrying each
// clock until it succeeds. Deep sleeps in between.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

#include "config.h"

static const char *TAG = "clocksync";

enum { EV_GOT_IP = BIT0, EV_BLE_READY = BIT1, EV_LINK_DONE = BIT2 };
static EventGroupHandle_t s_ev;

static RTC_DATA_ATTR bool s_time_ok;  // NTP has succeeded since power-up

// ===================== time helpers =====================

static uint32_t secs_to_midnight(void)
{
    time_t now = time(NULL);
    struct tm lt;
    localtime_r(&now, &lt);
    return 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec);
}

static void deep_sleep_s(uint32_t s)
{
    ESP_LOGI(TAG, "deep sleep %us", (unsigned)s);
    fflush(stdout);
    esp_deep_sleep((uint64_t)s * 1000000ULL);
}

// ===================== Wi-Fi + NTP =====================

static void wifi_cb(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT) {
        esp_wifi_connect();  // on START and on DISCONNECTED
    } else {
        xEventGroupSetBits(s_ev, EV_GOT_IP);
    }
}

static bool fetch_time(void)
{
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    wifi_config_t cfg = { .sta = { .ssid = WIFI_SSID, .password = WIFI_PASSWORD } };

    xEventGroupClearBits(s_ev, EV_GOT_IP);
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_START, wifi_cb, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, wifi_cb, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    bool ok = xEventGroupWaitBits(s_ev, EV_GOT_IP, pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_TIMEOUT_MS)) & EV_GOT_IP;
    if (ok) {
        esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG(NTP_HOST);
        esp_netif_sntp_init(&sntp);
        ok = false;
        for (int i = 0; i < NTP_TRIES && !ok; i++) {
            ok = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(5000)) == ESP_OK;
        }
        esp_netif_sntp_deinit();
    }

    esp_event_handler_unregister(WIFI_EVENT, WIFI_EVENT_STA_START, wifi_cb);
    esp_event_handler_unregister(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, wifi_cb);
    esp_event_handler_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_cb);
    esp_wifi_stop();
    esp_wifi_deinit();
    esp_netif_destroy_default_wifi(netif);

    ESP_LOGI(TAG, "time fetch %s", ok ? "ok" : "FAILED");
    return ok;
}

// ===================== BLE =====================

// EBE0CCB7-7A0A-4B0C-8A1A-6FF2997DA3A6 (little-endian)
static const ble_uuid128_t TIME_CHAR = BLE_UUID128_INIT(
    0xa6, 0xa3, 0x7d, 0x99, 0xf2, 0x6f, 0x1a, 0x8a,
    0x0c, 0x4b, 0x0a, 0x7a, 0xb7, 0xcc, 0xe0, 0xeb);

static const char *const MACS[] = CLOCK_MACS;
#define N_CLOCKS (sizeof(MACS) / sizeof(MACS[0]))

static uint8_t s_own_addr_type;
static uint16_t s_chr_handle;
static uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static bool s_wrote;

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_own_addr_type);
    xEventGroupSetBits(s_ev, EV_BLE_READY);
}

static void host_task(void *arg)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void hangup(uint16_t conn) { ble_gap_terminate(conn, BLE_ERR_REM_USER_CONN_TERM); }

static int write_cb(uint16_t conn, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg)
{
    s_wrote = err->status == 0;
    hangup(conn);
    return 0;
}

static int chr_cb(uint16_t conn, const struct ble_gatt_error *err, const struct ble_gatt_chr *chr, void *arg)
{
    if (err->status == 0 && chr) {
        s_chr_handle = chr->val_handle;
        return 0;
    }
    if (err->status == BLE_HS_EDONE && s_chr_handle) {
        // uint32 unix time (LE) + int8 timezone hours; time is taken now so
        // connection delays don't age it
        uint32_t ts = (uint32_t)time(NULL) + CLOCK_LEAD_SEC;
        uint8_t buf[5] = { ts, ts >> 8, ts >> 16, ts >> 24, TZ_HOURS };
        if (ble_gattc_write_flat(conn, s_chr_handle, buf, sizeof(buf), write_cb, NULL) == 0) {
            return 0;
        }
    }
    hangup(conn);
    return 0;
}

static int gap_cb(struct ble_gap_event *ev, void *arg)
{
    if (ev->type == BLE_GAP_EVENT_CONNECT) {
        if (ev->connect.status == 0) {
            s_conn = ev->connect.conn_handle;
            if (ble_gattc_disc_chrs_by_uuid(s_conn, 1, 0xffff, &TIME_CHAR.u, chr_cb, NULL) != 0) {
                hangup(s_conn);
            }
        } else {
            xEventGroupSetBits(s_ev, EV_LINK_DONE);
        }
    } else if (ev->type == BLE_GAP_EVENT_DISCONNECT) {
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        xEventGroupSetBits(s_ev, EV_LINK_DONE);
    }
    return 0;
}

static bool write_time(const char *mac)
{
    unsigned b[6];
    ble_addr_t addr = { .type = BLE_ADDR_PUBLIC };
    sscanf(mac, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]);
    for (int i = 0; i < 6; i++) {
        addr.val[5 - i] = b[i];  // NimBLE keeps the LSB first
    }

    s_wrote = false;
    s_chr_handle = 0;
    xEventGroupClearBits(s_ev, EV_LINK_DONE);
    if (ble_gap_connect(s_own_addr_type, &addr, CONNECT_TIMEOUT_MS, NULL, gap_cb, NULL) != 0) {
        return false;
    }
    if (!(xEventGroupWaitBits(s_ev, EV_LINK_DONE, pdFALSE, pdFALSE, pdMS_TO_TICKS(30000)) & EV_LINK_DONE)) {
        ble_gap_conn_cancel();
        if (s_conn != BLE_HS_CONN_HANDLE_NONE) {
            hangup(s_conn);
        }
    }
    return s_wrote;
}

// Round-robin over the clocks that are still pending, retrying until each
// has been written (or GIVE_UP_AFTER_S passes).
static void sync_clocks(void)
{
    ble_hs_cfg.sync_cb = on_sync;
    ESP_ERROR_CHECK(nimble_port_init());
    nimble_port_freertos_init(host_task);
    xEventGroupWaitBits(s_ev, EV_BLE_READY, pdFALSE, pdFALSE, portMAX_DELAY);

    bool done[N_CLOCKS] = { 0 };
    size_t pending = N_CLOCKS;
    int64_t give_up = esp_timer_get_time() + (int64_t)GIVE_UP_AFTER_S * 1000000;

    while (pending && esp_timer_get_time() < give_up) {
        for (size_t i = 0; i < N_CLOCKS; i++) {
            if (done[i]) {
                continue;
            }
            if (write_time(MACS[i])) {
                done[i] = true;
                pending--;
                ESP_LOGI(TAG, "%s synced", MACS[i]);
            } else {
                ESP_LOGW(TAG, "%s failed, will retry", MACS[i]);
                vTaskDelay(pdMS_TO_TICKS(RETRY_DELAY_S * 1000));
            }
        }
    }
    if (pending) {
        ESP_LOGE(TAG, "gave up on %u clock(s)", (unsigned)pending);
    }
}

// ===================== main =====================

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_ev = xEventGroupCreate();
    setenv("TZ", LOCAL_TZ, 1);
    tzset();

    // First boot (or a failed NTP last time): sync right away.
    // Otherwise: sleep in halves until just before midnight, so RTC drift
    // never accumulates over the day.
    bool now_run = !s_time_ok;
    if (!now_run && secs_to_midnight() > WAKE_WINDOW_S) {
        deep_sleep_s(secs_to_midnight() / 2);
    }

    if (!fetch_time()) {
        s_time_ok = false;
        deep_sleep_s(NTP_RETRY_S);
    }
    s_time_ok = true;

    // Within the window, wait for 00:00:00 (if NTP took us past it, go now)
    uint32_t wait = secs_to_midnight();
    if (!now_run && wait <= WAKE_WINDOW_S) {
        vTaskDelay(pdMS_TO_TICKS(wait * 1000));
    }

    sync_clocks();
    deep_sleep_s(secs_to_midnight() / 2);
}
