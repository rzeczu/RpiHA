#pragma once
// Interfejs 1 "RT-GH LED" (klasa 0xFF): EP 0x03 OUT od automatu, EP 0x82/0x84 IN do automatu.
#include <stdbool.h>
#include <stdint.h>
#include "tusb.h"

extern volatile uint32_t rtgh_led_packets;  // pakiety OUT od automatu

// Wysyła pakiet do automatu na endpoint IN interfejsu LED (0x82/0x84). false = zajęty / nieotwarty.
bool led_iface_in_send(uint8_t ep, uint8_t const *d, uint16_t n);
bool led_iface_in_busy(uint8_t ep);

// Haki (słabe; proxy_main.c je nadpisuje):
void rtgh_led_out_hook(uint8_t const *d, uint16_t n);  // każdy pakiet OUT od automatu
void rtgh_led_in_done_hook(uint8_t ep);                // automat odebrał pakiet IN
// Żądanie kontrolne vendor/class do interfejsu LED lub urządzenia. Zwraca -1 = obsłuż domyślnie
// (OUT: przyjmij i zaloguj, IN: STALL), 0 = STALL, 1 = obsłużone.
int rtgh_ctrl_hook(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req, uint8_t *buf, uint16_t buflen);
