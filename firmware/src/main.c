// RT-GH na Raspberry Pi Pico: emulacja gitary "RT GH CONTROLLER" (VID 0C70, PID 0777)
// do testów z automatem Guitar Hero Arcade.
//
// - USB: deskryptory, stringi i neutralny raport 1:1 z oryginału (rtgh_profile.h z profile.json).
// - Wejścia: przyciski zwierane do GND na pinach z pins.h (pull-up wewnętrzny).
// - Raport (7 B) wysyłany cyklicznie przy każdym odpytaniu hosta (bInterval 32 ms), jak oryginał.
// - Dioda na Pico: miga wolno = brak USB, świeci = podłączone, gaśnie przy wciśnięciu,
//   mrugnięcie = gra wysłała coś do interfejsu LED.
// - Log na UART0: GP0 = TX, 115200 8N1 (opcjonalnie, przez przejściówkę USB-UART).
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/structs/usb.h"
#include "hardware/adc.h"
#include "tusb.h"
#include "device/dcd.h"  // DCD_EVENT_* dla tud_event_hook_cb
#include "rtgh_profile.h"
#include "usblog.h"
#include "dump_mode.h"
#include "settings.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"

#define RTGH_FW_VERSION "1.0"
#define WATCHDOG_MS 1000  // zawieszenie pętli głównej > 1 s = restart (automat, nikt nie podejdzie z resetem)

// "Mating" (z nagrania proxy na automacie): gra wysyła na interfejs LED (EP 0x03) pakiet
// 03 0n (n = 0 dla P1, 1 dla P2), a gitara potwierdza to w raporcie HID: bajt 6, bity 4..7
// jednym zapalonym bitem 4+n (0x14 = P1, 0x24 = P2). Bez potwierdzenia gra powtarza 03 0n
// co 250 ms. Numer gracza zapamiętujemy we flashu.
#define PLAYER_BYTE  6
#define PLAYER_SHIFT 4
#define PLAYER_MASK  0xF0
static uint8_t player;
static volatile int8_t player_cmd = -1;   // ostatnia komenda 03 0n (z callbacku USB)
static uint32_t player_save_at;           // 0 = nic do zapisania

_Static_assert(RTGH_EP0_SIZE == CFG_TUD_ENDPOINT0_SIZE, "CFG_TUD_ENDPOINT0_SIZE musi równać się bMaxPacketSize0 oryginału");

extern volatile uint32_t rtgh_led_packets;

static bool pressed[RTGH_INPUT_COUNT];
static bool idle_high[RTGH_INPUT_COUNT];  // poziom w spoczynku (przyciski do GND: wysoki)
static uint8_t stable_ms[RTGH_INPUT_COUNT];
static uint8_t report[RTGH_REPORT_LEN];
static rtgh_stats_t stats, stats_logged;

static void set_field(uint8_t *r, uint8_t byte, uint8_t bit, uint8_t size, uint8_t value) {
    for (uint8_t k = 0; k < size; k++) {
        uint16_t pos = (uint16_t)bit + k;
        uint8_t *b = &r[byte + pos / 8];
        uint8_t mask = (uint8_t)(1u << (pos % 8));
        if ((value >> k) & 1u) *b |= mask;
        else *b &= (uint8_t)~mask;
    }
}

// ---- akcelerometr (3 kanały ADC), wygładzany filtrem IIR, wartości w jednostkach ADC 0..4095
#define ACCEL_RAW_MARK 0xA5  // bajt 4 w trybie kalibracji
static float accel[3];
static bool accel_raw_mode;

static void accel_init(void) {
    adc_init();
    for (int i = 0; i < 3; i++) {
        adc_gpio_init(ACCEL_ADC_FIRST_GPIO + i);
        adc_select_input((uint)i);
        accel[i] = (float)adc_read();
    }
}

static void accel_sample(void) {  // co 1 ms
    for (int i = 0; i < 3; i++) {
        adc_select_input((uint)i);
        uint32_t s = 0;
        for (int k = 0; k < 4; k++) s += adc_read();
        accel[i] += ((float)s * 0.25f - accel[i]) * 0.125f;
    }
}

static void accel_to_report(uint8_t *r) {
    if (accel_raw_mode) {
        // kalibracja: trzy wartości 10-bit (ADC>>2) w bajtach 0..3, znacznik w bajcie 4
        uint32_t v = 0;
        for (int i = 0; i < 3; i++) {
            uint32_t a = (uint32_t)(accel[i] + 0.5f) >> 2;
            if (a > 1023) a = 1023;
            v |= a << (10 * i);
        }
        for (int i = 0; i < 4; i++) r[i] = (uint8_t)(v >> (8 * i));
        r[4] = ACCEL_RAW_MARK;
        return;
    }
#if RTGH_ACCEL_COUNT > 0
    for (int k = 0; k < RTGH_ACCEL_COUNT; k++) {
        float const *c = rtgh_accel_coef[k];
        float v = c[0] + c[1] * accel[0] + c[2] * accel[1] + c[3] * accel[2];
        if (v > 127.f) v = 127.f;
        if (v < -127.f) v = -127.f;
        int iv = (int)(v + (v >= 0 ? 0.5f : -0.5f));
        r[rtgh_accel_byte[k]] = (uint8_t)(int8_t)iv;
    }
#endif
}

// krótkie naciśnięcie BOOTSEL: włącz/wyłącz tryb kalibracji akcelerometru
void rtgh_bootsel_short_press(void) {
    accel_raw_mode = !accel_raw_mode;
    usblog_text(accel_raw_mode ? "accel raw mode ON (BOOTSEL)" : "accel raw mode OFF (BOOTSEL)");
    printf("Tryb kalibracji akcelerometru: %s\n", accel_raw_mode ? "wl" : "wyl");
}

void rtgh_led_out_hook(uint8_t const *d, uint16_t n) {
    if (n >= 2 && d[0] == 0x03 && d[1] < 4) player_cmd = (int8_t)d[1];
}

static void build_report(uint8_t *r) {
    memcpy(r, rtgh_neutral_report, RTGH_REPORT_LEN);
#if RTGH_REPORT_LEN > PLAYER_BYTE
    r[PLAYER_BYTE] = (uint8_t)((r[PLAYER_BYTE] & ~PLAYER_MASK) | (1u << (PLAYER_SHIFT + player)));
#endif
    accel_to_report(r);
    for (int i = 0; i < RTGH_INPUT_COUNT; i++) {
        if (!pressed[i]) continue;
        rtgh_input_t const *in = &rtgh_inputs[i];
        if (in->size > 1) {
            // pole wielobitowe (hat: strum góra/dół) - dwa naraz = zostaw neutralne
            bool other = false;
            for (int j = 0; j < RTGH_INPUT_COUNT; j++) {
                rtgh_input_t const *o = &rtgh_inputs[j];
                if (j != i && pressed[j] && o->byte == in->byte && o->bit == in->bit && o->size == in->size) other = true;
            }
            if (other) continue;
        }
        set_field(r, in->byte, in->bit, in->size, in->value);
    }
}

static bool scan_inputs(void) {
    bool changed = false;
    for (int i = 0; i < RTGH_INPUT_COUNT; i++) {
        bool raw = gpio_get(rtgh_inputs[i].pin) != idle_high[i];  // inny niż spoczynkowy = wciśnięte
        if (raw == pressed[i]) {
            stable_ms[i] = 0;
        } else if (++stable_ms[i] >= DEBOUNCE_MS) {
            pressed[i] = raw;
            stable_ms[i] = 0;
            changed = true;
            printf("%-10s %s\n", rtgh_inputs[i].name, raw ? "WCISNIETY" : "puszczony");
        }
    }
    if (changed) {
        uint8_t mask = 0;
        for (int i = 0; i < RTGH_INPUT_COUNT && i < 8; i++)
            if (pressed[i]) mask |= (uint8_t)(1u << i);
        usblog_add(LOG_INPUTS, &mask, 1);
    }
    return changed;
}

// Wywoływane przez TinyUSB przy każdym zdarzeniu (także w przerwaniu) - do logu.
void tud_event_hook_cb(uint8_t rhport, uint32_t eventid, bool in_isr) {
    (void)rhport;
    (void)in_isr;
    switch (eventid) {
        case DCD_EVENT_BUS_RESET: usblog_add(LOG_BUS_RESET, NULL, 0); break;
        case DCD_EVENT_UNPLUGGED: usblog_add(LOG_UNPLUGGED, NULL, 0); break;
        case DCD_EVENT_SUSPEND: usblog_add(LOG_SUSPEND, NULL, 0); break;
        case DCD_EVENT_RESUME: usblog_add(LOG_RESUME, NULL, 0); break;
        case DCD_EVENT_SETUP_RECEIVED: {
            uint8_t s[8];
            for (int i = 0; i < 8; i++) s[i] = usb_dpram->setup_packet[i];
            usblog_add(LOG_SETUP, s, 8);
            break;
        }
        default: break;
    }
}

// Czy host odpytuje endpointy IN, na które nic nie wysyłamy (NAK). Bity: EPn IN = bit 2n.
static void poll_naks(void) {
    uint32_t st = usb_hw->ep_nak_stall_status;
    if (!st) return;
    usb_hw->ep_nak_stall_status = st;  // W1C
    for (int n = 0; n < 8; n++)
        if (st & (1u << (2 * n))) stats.nak_in[n]++;
}

static void log_stats(void) {
    stats.dropped = usblog_dropped();
    if (memcmp(&stats, &stats_logged, sizeof(stats)) == 0) return;
    usblog_add(LOG_STATS, &stats, sizeof(stats));
    stats_logged = stats;
}

static bool any_pressed(void) {
    for (int i = 0; i < RTGH_INPUT_COUNT; i++)
        if (pressed[i]) return true;
    return false;
}

void tud_mount_cb(void) {
    printf("USB: skonfigurowane przez hosta\n");
    usblog_add(LOG_MOUNT, NULL, 0);
    if (rtgh_dump_mode) return;
    usb_dpram->ep_ctrl[(RTGH_HID_EP_IN & 0x0F) - 1].in |= EP_CTRL_INTERRUPT_ON_NAK;
}
void tud_umount_cb(void) {
    printf("USB: odłączone\n");
    usblog_add(LOG_UNMOUNT, NULL, 0);
}
void tud_suspend_cb(bool remote_wakeup_en) { (void)remote_wakeup_en; printf("USB: suspend\n"); }
void tud_resume_cb(void) { printf("USB: resume\n"); }

// GET_REPORT (np. HidD_GetInputReport) - zwracamy bieżący raport
uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen) {
    (void)instance;
    uint8_t l[2] = {report_id, (uint8_t)report_type};
    usblog_add(LOG_HID_GET_REPORT, l, 2);
    if (report_type != HID_REPORT_TYPE_INPUT) return 0;  // STALL
    uint8_t r[RTGH_REPORT_LEN];
    build_report(r);
    uint16_t n = reqlen < RTGH_REPORT_LEN ? reqlen : RTGH_REPORT_LEN;
    memcpy(buffer, r, n);
    return n;
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize) {
    (void)instance;
    printf("HID SET_REPORT id=%u typ=%u (%u B):", report_id, report_type, bufsize);
    for (uint16_t i = 0; i < bufsize; i++) printf(" %02x", buffer[i]);
    printf("\n");
    uint8_t l[2 + 62];
    uint16_t n = bufsize < 62 ? bufsize : 62;
    l[0] = report_id;
    l[1] = (uint8_t)report_type;
    memcpy(l + 2, buffer, n);
    usblog_add(LOG_HID_SET_REPORT, l, (uint8_t)(2 + n));
}

int main(void) {
    stdio_init_all();  // tylko UART (stdio_usb wyłączone w CMakeLists - USB zajmuje TinyUSB)

    for (int i = 0; i < RTGH_INPUT_COUNT; i++) {
        gpio_init(rtgh_inputs[i].pin);
        gpio_set_dir(rtgh_inputs[i].pin, GPIO_IN);
        gpio_pull_up(rtgh_inputs[i].pin);
    }
#ifdef PICO_DEFAULT_LED_PIN
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
#endif
    gpio_init(PIN_DUMP);
    gpio_set_dir(PIN_DUMP, GPIO_IN);
    gpio_pull_up(PIN_DUMP);
    sleep_ms(20);  // pull-upy się ustalają (membrany + długie przewody)

    // Tryby serwisowe dla zakręconej gitary (BOOTSEL niedostępny) - progi trzymane przy podłączaniu:
    //   GREEN + RED      -> kalibracja akcelerometru (niżej)
    //   BLUE + ORANGE    -> odczyt logu (port COM)
    //   GREEN + ORANGE   -> bootloader UF2 (dysk RPI-RP2, wgrywanie firmware'u przez kabel gitary)
    uint8_t frets = 0;
    {
        static const uint8_t fp[5] = {PIN_GREEN, PIN_RED, PIN_YELLOW, PIN_BLUE, PIN_ORANGE};
        for (int i = 0; i < 5; i++)
            if (!gpio_get(fp[i])) frets |= (uint8_t)(1u << i);
    }
    enum { F_G = 1, F_R = 2, F_Y = 4, F_B = 8, F_O = 16 };
    if (frets == (F_G | F_O)) {
        reset_usb_boot(0, 0);  // nie wraca
    }

    // zworka GP9-GND, BLUE+ORANGE albo restart po przytrzymaniu BOOTSEL: tryb zrzutu logu zamiast gitary
    if (dump_requested_at_boot() || !gpio_get(PIN_DUMP) || frets == (F_B | F_O)) {
        rtgh_dump_mode = true;
        dump_mode_run();
    }

    // Poziom spoczynkowy wejść. Progi/przyciski: zawsze wysoki (pull-up, zwarcie do GND).
    // Strum (czujnik optyczny może mieć odwrotną logikę): to, co jest na pinie przez 50 ms po starcie.
    char strum_info[48] = "";
    for (int i = 0; i < RTGH_INPUT_COUNT; i++) {
        idle_high[i] = true;
        uint8_t pin = rtgh_inputs[i].pin;
        if (STRUM_AUTO_POLARITY && (pin == PIN_STRUM_UP || pin == PIN_STRUM_DOWN)) {
            int highs = 0;
            for (int k = 0; k < 50; k++) {
                highs += gpio_get(pin);
                sleep_ms(1);
            }
            idle_high[i] = highs >= 25;
            size_t l = strlen(strum_info);
            snprintf(strum_info + l, sizeof(strum_info) - l, " %s=%s", pin == PIN_STRUM_UP ? "up" : "down",
                     idle_high[i] ? "H" : "L");
        }
    }

    accel_init();
    // GREEN + RED trzymane przy podłączaniu: tryb kalibracji akcelerometru (surowe ADC w raporcie)
    accel_raw_mode = frets == (F_G | F_R);

    usblog_init();  // przed USB: przy pierwszym starcie kasuje obszar logu (~2 s)
    if (accel_raw_mode) usblog_text("accel raw mode (kalibracja)");
    {
        char t[64];
        snprintf(t, sizeof(t), "accel ADC0..2 = %d %d %d", (int)accel[0], (int)accel[1], (int)accel[2]);
        usblog_text(t);
    }
    usblog_text("boot rtgh_pico " RTGH_FW_VERSION " " __DATE__ " " __TIME__);
    if (watchdog_enable_caused_reboot()) usblog_text("UWAGA: poprzedni restart wymusil watchdog (zawieszenie)");
    player = settings_load_player();
    {
        char t[24];
        snprintf(t, sizeof(t), "player P%u", player + 1);
        usblog_text(t);
        printf("Gracz: P%u\n", player + 1);
    }
    if (strum_info[0]) {
        char t[64];
        snprintf(t, sizeof(t), "strum idle:%s", strum_info);
        usblog_text(t);
        printf("%s\n", t);
    }

    tud_init(BOARD_TUD_RHPORT);
    watchdog_enable(WATCHDOG_MS, true);

    printf("\nRT-GH Pico: VID %02x%02x PID %02x%02x, raport %d B\n", rtgh_desc_device[9], rtgh_desc_device[8],
           rtgh_desc_device[11], rtgh_desc_device[10], RTGH_REPORT_LEN);
    for (int i = 0; i < RTGH_INPUT_COUNT; i++)
        printf("  %-10s GP%-2u -> bajt %u bit %u (%u b) = %u\n", rtgh_inputs[i].name, rtgh_inputs[i].pin,
               rtgh_inputs[i].byte, rtgh_inputs[i].bit, rtgh_inputs[i].size, rtgh_inputs[i].value);

    absolute_time_t next_scan = make_timeout_time_ms(1);
    absolute_time_t next_stats = make_timeout_time_ms(2000);
    uint32_t led_seen = 0;
    absolute_time_t led_flash_until = get_absolute_time();

    while (true) {
        watchdog_update();
        tud_task();
        poll_naks();

        if (time_reached(next_scan)) {
            next_scan = delayed_by_ms(next_scan, 1);
            scan_inputs();
            accel_sample();
        }

        // Jak oryginał: raport w każdym odpytaniu hosta (co bInterval), także bez zmian.
        if (tud_mounted() && tud_hid_ready()) {
            build_report(report);
            if (tud_hid_report(0, report, RTGH_REPORT_LEN)) stats.hid_reports++;
        }

        if (time_reached(next_stats)) {
            next_stats = make_timeout_time_ms(2000);
            stats.led_out = rtgh_led_packets;
            log_stats();
        }
        // mating: nowy numer gracza -> od razu w raporcie, do flasha po 1 s spokoju
        if (player_cmd >= 0) {
            uint8_t p = (uint8_t)player_cmd;
            player_cmd = -1;
            if (p != player) {
                player = p;
                char t[24];
                snprintf(t, sizeof(t), "player set P%u", p + 1);
                usblog_text(t);
                printf("Mating: gracz P%u\n", p + 1);
                player_save_at = to_ms_since_boot(get_absolute_time()) + 1000;
                if (!player_save_at) player_save_at = 1;
            }
        }
        if (player_save_at && (int32_t)(to_ms_since_boot(get_absolute_time()) - player_save_at) >= 0) {
            player_save_at = 0;
            if (settings_load_player() != player) settings_save_player(player);
        }

        usblog_task();
        dump_bootsel_poll();

#ifdef PICO_DEFAULT_LED_PIN
        if (rtgh_led_packets != led_seen) {
            led_seen = rtgh_led_packets;
            led_flash_until = make_timeout_time_ms(40);
        }
        bool on;
        if (!tud_mounted()) on = (to_ms_since_boot(get_absolute_time()) / 500) & 1;  // wolne miganie
        else if (accel_raw_mode) on = (to_ms_since_boot(get_absolute_time()) / 250) & 1;  // 2 Hz: kalibracja
        else on = !any_pressed();
        if (!time_reached(led_flash_until)) on = !on;
        if (usblog_full()) on = (to_ms_since_boot(get_absolute_time()) / 100) & 1;  // szybkie miganie: log pełny
        gpio_put(PICO_DEFAULT_LED_PIN, on);
#endif
    }
}
