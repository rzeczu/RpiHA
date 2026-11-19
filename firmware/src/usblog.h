#pragma once
// Log zdarzeń USB w pamięci flash Pico (przetrwa odłączenie zasilania).
// Odczyt: Pico w trybie zrzutu (GP9 zwarty do GND przy podłączaniu) -> port COM -> read_pico_log.py.
#include <stdbool.h>
#include <stdint.h>

enum {
    LOG_BOOT = 1,         // tekst: wersja firmware'u
    LOG_BUS_RESET,
    LOG_UNPLUGGED,
    LOG_SUSPEND,
    LOG_RESUME,
    LOG_SETUP,            // 8 B pakietu SETUP (każde żądanie kontrolne hosta)
    LOG_MOUNT,            // SET_CONFIGURATION zakończone
    LOG_UNMOUNT,
    LOG_LED_OUT,          // dane z hosta na interfejs LED (tylko gdy inne niż poprzednie)
    LOG_CTRL_OUT_DATA,    // dane etapu DATA żądania kontrolnego OUT (vendor/class do iface LED)
    LOG_HID_SET_REPORT,   // [report_id, typ, dane...]
    LOG_HID_GET_REPORT,   // [report_id, typ]
    LOG_STATS,            // rtgh_stats_t, co 2 s jeśli coś się zmieniło
    LOG_INPUTS,           // [maska wciśniętych wejść]
    LOG_TEXT,             // dowolny tekst
    // tylko proxy (oryginalna gitara na porcie PIO-USB):
    LOG_G_IN,             // 16: [ep][dane] gitara -> automat na 0x82/0x84 (gdy inne niż poprzednie na tym EP)
    LOG_G_HID,            // 17: raport HID gitary (gdy zmiana poza szumiącymi bajtami)
    LOG_G_CTRL,           // 18: [setup 8][wynik][dane] żądanie kontrolne przekazane do gitary
    LOG_PSTATS,           // 19: rtgh_pstats_t co 2 s, jeśli coś się zmieniło
};

typedef struct __attribute__((packed)) {
    uint32_t out_fwd;       // pakiety automat -> gitara (EP 0x03) przekazane
    uint32_t out_drop;      // ... zgubione (gitara niepodłączona / kolejka pełna)
    uint32_t g_in[2];       // pakiety od gitary na 0x82, 0x84
    uint32_t in_sent[2];    // ... odebrane przez automat
    uint32_t in_drop[2];    // ... zgubione (automat nie odbierał, kolejka pełna)
    uint32_t g_hid;         // raporty HID od gitary
    uint32_t ctrl_fwd;      // żądania kontrolne przekazane do gitary
    uint32_t g_err;         // błędy transferów po stronie gitary
} rtgh_pstats_t;

typedef struct __attribute__((packed)) {
    uint32_t hid_reports;   // raporty HID wysłane do hosta
    uint32_t led_out;       // pakiety OUT na interfejs LED
    uint32_t nak_in[8];     // ile razy (co najmniej) host odpytał endpoint IN n i dostał NAK
    uint32_t dropped;       // rekordy zgubione (pełny bufor RAM)
} rtgh_stats_t;

uint32_t usblog_dropped(void);

void usblog_init(void);                                   // przed tud_init(); może skasować obszar logu
void usblog_add(uint8_t type, const void *data, uint8_t len);  // bezpieczne też w przerwaniu
void usblog_text(const char *s);
void usblog_task(void);                                   // w pętli głównej: zapis stron do flash
bool usblog_full(void);
const uint8_t *usblog_region(uint32_t *addr, uint32_t *size);  // cały obszar logu we flashu (XIP)
void usblog_clear(void);                                  // kasuje nagłówek -> log od nowa przy następnym starcie
