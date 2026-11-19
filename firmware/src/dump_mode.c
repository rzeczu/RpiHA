// Tryb zrzutu logu przez USB CDC (wirtualny port COM).
// Komendy (1 bajt od hosta):
//   'I' -> linia tekstu z adresem i rozmiarem obszaru logu
//   'D' -> "RTGHDUMP" + adres u32 LE + rozmiar u32 LE + surowy obszar logu z flasha
//   'C' -> kasuje nagłówek logu (pełne kasowanie przy następnym normalnym starcie), odpowiedź "CLEARED\n"
// Inny VID/PID niż gitara (2E8A:000A jak port szeregowy Pico SDK), więc Windows nie myli go z gitarą.
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "tusb.h"
#include "hardware/sync.h"
#include "hardware/watchdog.h"
#include "hardware/structs/ioqspi.h"
#include "hardware/structs/sio.h"
#include "usblog.h"
#include "dump_mode.h"

bool rtgh_dump_mode;

#define DUMP_MAGIC 0x52544744u  // "RTGD" w watchdog scratch[0]

// Odczyt BOOTSEL (linia CS flasha) - funkcja w RAM, bo na chwilę odcina flash.
static bool __no_inline_not_in_flash_func(bootsel_pressed)(void) {
    const uint CS_PIN_INDEX = 1;
    uint32_t flags = save_and_disable_interrupts();
    hw_write_masked(&ioqspi_hw->io[CS_PIN_INDEX].ctrl, GPIO_OVERRIDE_LOW << IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_LSB,
                    IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_BITS);
    for (volatile int i = 0; i < 1000; ++i) {
    }
#if PICO_RP2040
    const uint32_t CS_BIT = 1u << 1;
#else
    const uint32_t CS_BIT = SIO_GPIO_HI_IN_QSPI_CSN_BITS;
#endif
    bool pressed = !(sio_hw->gpio_hi_in & CS_BIT);
    hw_write_masked(&ioqspi_hw->io[CS_PIN_INDEX].ctrl, GPIO_OVERRIDE_NORMAL << IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_LSB,
                    IO_QSPI_GPIO_QSPI_SS_CTRL_OEOVER_BITS);
    restore_interrupts(flags);
    return pressed;
}

__attribute__((weak)) void rtgh_bootsel_short_press(void) {}

void dump_bootsel_poll(void) {
    static uint32_t next_ms, held;
    uint32_t now = to_ms_since_boot(get_absolute_time());
    if ((int32_t)(now - next_ms) < 0) return;
    next_ms = now + 100;
    if (bootsel_pressed()) {
        held++;
    } else {
        if (held >= 1 && held <= 7) rtgh_bootsel_short_press();  // krótkie naciśnięcie (0,1-0,7 s)
        held = 0;
    }
    if (held >= 10) {  // ~1 s
        usblog_text("BOOTSEL held -> reboot to dump mode");
        uint32_t t0 = to_ms_since_boot(get_absolute_time());
        while (to_ms_since_boot(get_absolute_time()) - t0 < 400) usblog_task();  // zapisz zaległy log
        watchdog_hw->scratch[0] = DUMP_MAGIC;
        watchdog_reboot(0, 0, 10);
        while (true) tight_loop_contents();
    }
}

bool dump_requested_at_boot(void) {
    if (watchdog_hw->scratch[0] != DUMP_MAGIC) return false;
    watchdog_hw->scratch[0] = 0;
    return true;
}

static tusb_desc_device_t const dev = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0x2E8A,
    .idProduct = 0x000A,
    .bcdDevice = 0x0100,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 3,
    .bNumConfigurations = 1,
};

static uint8_t const cfg[] = {
    TUD_CONFIG_DESCRIPTOR(1, 2, 0, TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN, 0, 100),
    TUD_CDC_DESCRIPTOR(0, 0, 0x81, 8, 0x02, 0x82, 64),
};

static char const *const strs[] = {NULL, "RT-GH Pico", "RT-GH Pico log dump", "RTGHDUMP1"};
static uint16_t str_buf[40];

uint8_t const *dump_desc_device(void) { return (uint8_t const *)&dev; }
uint8_t const *dump_desc_config(void) { return cfg; }

uint16_t const *dump_desc_string(uint8_t index) {
    uint8_t n;
    if (index == 0) {
        str_buf[1] = 0x0409;
        n = 1;
    } else if (index < sizeof(strs) / sizeof(strs[0])) {
        char const *s = strs[index];
        for (n = 0; s[n] && n < 39; n++) str_buf[1 + n] = (uint8_t)s[n];
    } else {
        return NULL;
    }
    str_buf[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * n + 2));
    return str_buf;
}

static void led_task(void) {
#ifdef PICO_DEFAULT_LED_PIN
    // podwójne mrugnięcie co sekundę = tryb zrzutu
    uint32_t ph = (to_ms_since_boot(get_absolute_time()) / 100) % 10;
    gpio_put(PICO_DEFAULT_LED_PIN, ph == 0 || ph == 2);
#endif
}

static void send_all(uint8_t const *p, uint32_t n) {
    while (n) {
        tud_task();
        led_task();
        if (!tud_cdc_connected()) return;
        uint32_t a = tud_cdc_write_available();
        if (a == 0) continue;
        uint32_t k = n < a ? n : a;
        k = tud_cdc_write(p, k);
        p += k;
        n -= k;
        tud_cdc_write_flush();
    }
    tud_cdc_write_flush();
}

static void send_text(char const *s) { send_all((uint8_t const *)s, (uint32_t)strlen(s)); }

void dump_mode_run(void) {
    hw_clear_bits(&watchdog_hw->ctrl, WATCHDOG_CTRL_ENABLE_BITS);  // zrzut może trwać - bez watchdoga
    printf("Tryb zrzutu logu (GP9 zwarty do GND przy starcie)\n");
    tud_init(BOARD_TUD_RHPORT);
    while (true) {
        tud_task();
        led_task();
        if (!tud_cdc_available()) continue;
        int c = tud_cdc_read_char();
        uint32_t addr, size;
        uint8_t const *log = usblog_region(&addr, &size);
        if (c == 'I') {
            char line[96];
            snprintf(line, sizeof(line), "RTGH dump v1 addr=0x%08lx size=%lu magic=%s\n", (unsigned long)addr,
                     (unsigned long)size, memcmp(log, "RTGHLOG1", 8) == 0 ? "ok" : "brak");
            send_text(line);
        } else if (c == 'D') {
            uint8_t hdr[16];
            memcpy(hdr, "RTGHDUMP", 8);
            memcpy(hdr + 8, &addr, 4);
            memcpy(hdr + 12, &size, 4);
            send_all(hdr, sizeof(hdr));
            send_all(log, size);
        } else if (c == 'C') {
            usblog_clear();
            send_text("CLEARED\n");
        }
    }
}
