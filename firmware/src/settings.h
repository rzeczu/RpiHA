#pragma once
// Trwałe ustawienia emulatora (sektor flasha tuż przed obszarem logu).
#include <stdint.h>

uint8_t settings_load_player(void);        // 0 = P1 (domyślnie), 1 = P2, ...
void settings_save_player(uint8_t player); // kasuje i zapisuje sektor (~50 ms bez przerwań)
