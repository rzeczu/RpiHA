#pragma once
// Tryb zrzutu logu: GP9 (PIN_DUMP) zwarty do GND przy podłączaniu Pico.
// Pico zgłasza się wtedy jako port szeregowy USB (COM, sterownik wbudowany w Windows),
// a read_pico_log.py pobiera przez niego cały obszar logu z flasha.
#include <stdbool.h>
#include <stdint.h>

extern bool rtgh_dump_mode;

uint8_t const *dump_desc_device(void);
uint8_t const *dump_desc_config(void);
uint16_t const *dump_desc_string(uint8_t index);
void dump_mode_run(void);  // nie wraca

// Drugi sposób wejścia w tryb zrzutu: BOOTSEL przytrzymany ~1 s, gdy firmware już działa
// -> restart do trybu zrzutu. Wołać co ~100 ms z pętli głównej.
void dump_bootsel_poll(void);
void rtgh_bootsel_short_press(void);  // słaby hak: krótkie naciśnięcie BOOTSEL (main.c: tryb kalibracji)
bool dump_requested_at_boot(void);  // flaga po takim restarcie (kasuje ją)
