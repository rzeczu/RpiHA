// Log zdarzeń USB w ostatnich 512 KB flasha.
// Format: sektor nagłówka ("RTGHLOG1", rozmiar obszaru), potem strony 256 B z rekordami
// [typ u8][len u8][czas_ms u32 LE][dane len B]; typ 0xFF = koniec danych w stronie.
// Rekordy trafiają do bufora RAM (także z przerwań), pętla główna zapisuje całe strony.
// Kasowanie obszaru tylko przy starcie (pierwsze uruchomienie albo prawie pełny log).
#include <string.h>
#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/flash.h"
#include "hardware/sync.h"
#include "pico/critical_section.h"
#include "usblog.h"

#define LOG_REGION_SIZE (512u * 1024u)
#define LOG_REGION_OFF  (PICO_FLASH_SIZE_BYTES - LOG_REGION_SIZE)
#define LOG_DATA_OFF    (LOG_REGION_OFF + FLASH_SECTOR_SIZE)
#define LOG_END_OFF     (LOG_REGION_OFF + LOG_REGION_SIZE)
#define LOG_MAGIC       "RTGHLOG1"
#define LOG_MIN_FREE    (64u * 1024u)   // mniej wolnego przy starcie -> kasuj i zacznij od nowa

#define RING_SIZE 16384u                // potęga dwójki
#define RING_MASK (RING_SIZE - 1u)
#define REC_HDR   6u
#define REC_MAX   80u

// Spinlock + wyłączone przerwania: usblog_add bezpieczne z przerwań i z obu rdzeni (proxy: rdzeń 1 = host).
static critical_section_t cs;
static bool inited;
static uint8_t ring[RING_SIZE];
static volatile uint32_t r_head, r_tail;   // rosnące liczniki; head pisze usblog_add, tail czyta usblog_task
static volatile uint32_t dropped;
static volatile uint32_t last_add_ms, first_pending_ms;
static uint32_t wr_off;
static bool full;
static uint8_t page[FLASH_PAGE_SIZE];

_Static_assert(LOG_REGION_OFF % FLASH_BLOCK_SIZE == 0, "obszar logu musi zaczynać się na granicy bloku");

static inline const uint8_t *flash_ptr(uint32_t off) {
    return (const uint8_t *)(XIP_BASE + off);
}

static void erase_all(void) {
    printf("LOG: kasuję obszar logu (%u KB)...\n", LOG_REGION_SIZE / 1024);
    memset(page, 0xFF, sizeof(page));
    memcpy(page, LOG_MAGIC, 8);
    uint32_t size = LOG_REGION_SIZE;
    memcpy(page + 8, &size, 4);
    uint32_t ints = save_and_disable_interrupts();
    flash_range_erase(LOG_REGION_OFF, LOG_REGION_SIZE);
    flash_range_program(LOG_REGION_OFF, page, FLASH_PAGE_SIZE);
    restore_interrupts(ints);
    wr_off = LOG_DATA_OFF;
}

void usblog_init(void) {
    critical_section_init(&cs);
    inited = true;
    if (memcmp(flash_ptr(LOG_REGION_OFF), LOG_MAGIC, 8) != 0) {
        erase_all();
    } else {
        wr_off = LOG_END_OFF;
        for (uint32_t off = LOG_DATA_OFF; off < LOG_END_OFF; off += FLASH_PAGE_SIZE) {
            if (flash_ptr(off)[0] == 0xFF) {
                wr_off = off;
                break;
            }
        }
        if (LOG_END_OFF - wr_off < LOG_MIN_FREE) erase_all();
    }
    printf("LOG: zapis od 0x%08lx, wolne %lu KB\n", (unsigned long)(XIP_BASE + wr_off),
           (unsigned long)((LOG_END_OFF - wr_off) / 1024));
}

void usblog_add(uint8_t type, const void *data, uint8_t len) {
    if (!inited) return;  // tryb zrzutu: logu nie piszemy
    if (len > REC_MAX) len = REC_MAX;
    uint8_t rec[REC_HDR + REC_MAX];
    uint32_t t = to_ms_since_boot(get_absolute_time());
    rec[0] = type;
    rec[1] = len;
    memcpy(rec + 2, &t, 4);
    if (len) memcpy(rec + REC_HDR, data, len);
    uint32_t n = REC_HDR + len;

    critical_section_enter_blocking(&cs);
    uint32_t used = r_head - r_tail;
    if (used + n > RING_SIZE) {
        dropped++;
    } else {
        if (used == 0) first_pending_ms = t;
        for (uint32_t i = 0; i < n; i++) ring[(r_head + i) & RING_MASK] = rec[i];
        r_head += n;
        last_add_ms = t;
    }
    critical_section_exit(&cs);
}

void usblog_text(const char *s) {
    usblog_add(LOG_TEXT, s, (uint8_t)strnlen(s, REC_MAX));
}

bool usblog_full(void) {
    return full;
}

uint32_t usblog_dropped(void) {
    return dropped;
}

const uint8_t *usblog_region(uint32_t *addr, uint32_t *size) {
    *addr = XIP_BASE + LOG_REGION_OFF;
    *size = LOG_REGION_SIZE;
    return flash_ptr(LOG_REGION_OFF);
}

void usblog_clear(void) {
    // tylko sektor nagłówka - następny normalny start zobaczy brak "RTGHLOG1" i skasuje resztę
    uint32_t ints = save_and_disable_interrupts();
    flash_range_erase(LOG_REGION_OFF, FLASH_SECTOR_SIZE);
    restore_interrupts(ints);
}

void usblog_task(void) {
    if (!inited) return;
    uint32_t used = r_head - r_tail;
    if (used == 0) return;
    if (full) {  // nie ma gdzie pisać - wyrzucamy
        critical_section_enter_blocking(&cs);
        r_tail = r_head;
        critical_section_exit(&cs);
        return;
    }
    uint32_t now = to_ms_since_boot(get_absolute_time());
    bool page_worth = used >= FLASH_PAGE_SIZE - (REC_HDR + REC_MAX);
    bool quiet = now - last_add_ms > 300;          // chwila ciszy: zapisz, zanim ktoś odłączy Pico
    bool old = now - first_pending_ms > 2000;      // albo dane czekają już 2 s
    if (!page_worth && !quiet && !old) return;

    memset(page, 0xFF, sizeof(page));
    uint32_t p = 0;
    critical_section_enter_blocking(&cs);
    uint32_t tail = r_tail;
    while (r_head - tail > 0) {
        uint32_t n = REC_HDR + ring[(tail + 1) & RING_MASK];
        if (p + n > FLASH_PAGE_SIZE) break;
        for (uint32_t i = 0; i < n; i++) page[p + i] = ring[(tail + i) & RING_MASK];
        p += n;
        tail += n;
    }
    r_tail = tail;
    if (r_head != r_tail) first_pending_ms = now;
    critical_section_exit(&cs);
    // Zapis z wyłączonymi przerwaniami tego rdzenia. W proxy drugi rdzeń działa dalej, więc cały
    // firmware proxy jest uruchamiany z RAM (copy_to_ram) - nic nie czyta wtedy z flasha.
    uint32_t ints = save_and_disable_interrupts();
    flash_range_program(wr_off, page, FLASH_PAGE_SIZE);
    restore_interrupts(ints);
    wr_off += FLASH_PAGE_SIZE;
    if (wr_off >= LOG_END_OFF) {
        full = true;
        printf("LOG: obszar pełny - dalsze zdarzenia nie są zapisywane\n");
    }
}
