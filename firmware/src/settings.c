// Numer gracza z "matingu" (komenda 03 0n na interfejsie LED) przechowywany we flashu,
// jak w oryginalnej gitarze - po wyłączeniu zasilania gitara dalej zgłasza tego samego gracza.
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/flash.h"
#include "hardware/sync.h"
#include "settings.h"

#define CFG_OFF   (PICO_FLASH_SIZE_BYTES - 512u * 1024u - FLASH_SECTOR_SIZE)  // przed logiem (usblog.c)
#define CFG_MAGIC "RTGHCFG1"

uint8_t settings_load_player(void) {
    const uint8_t *p = (const uint8_t *)(XIP_BASE + CFG_OFF);
    if (memcmp(p, CFG_MAGIC, 8) != 0 || p[8] > 3) return 0;
    return p[8];
}

void settings_save_player(uint8_t player) {
    static uint8_t page[FLASH_PAGE_SIZE];
    memset(page, 0xFF, sizeof(page));
    memcpy(page, CFG_MAGIC, 8);
    page[8] = player;
    uint32_t ints = save_and_disable_interrupts();
    flash_range_erase(CFG_OFF, FLASH_SECTOR_SIZE);
    flash_range_program(CFG_OFF, page, FLASH_PAGE_SIZE);
    restore_interrupts(ints);
}
