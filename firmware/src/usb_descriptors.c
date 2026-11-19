// Deskryptory USB - wszystkie bajty pochodzą z nagrania oryginalnej gitary (rtgh_profile.h).
// W trybie zrzutu logu (dump_mode.c) zamiast gitary zgłasza się port szeregowy.
#include <string.h>
#include "tusb.h"
#include "hardware/structs/usb.h"
#include "rtgh_profile.h"
#include "dump_mode.h"

uint8_t const *tud_descriptor_device_cb(void) {
    if (rtgh_dump_mode) return dump_desc_device();
    return rtgh_desc_device;
}

uint8_t const *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    if (rtgh_dump_mode) return dump_desc_config();
    // TinyUSB woła ten callback także przy SET_CONFIGURATION i wtedy sam parsuje deskryptor.
    // Oryginał ma po nagłówku deskryptor OTG (03 09 00), na którym parser TinyUSB robi assert.
    // Rozróżniamy po bieżącym pakiecie SETUP w DPRAM: host przy GET_DESCRIPTOR dostaje
    // oryginał bajt w bajt, parser przy SET_CONFIGURATION (bRequest 0x09) wersję bez OTG.
    if (usb_dpram->setup_packet[1] == 0x09) {
        return rtgh_desc_config_tusb;
    }
    return rtgh_desc_config;
}

uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance) {
    (void)instance;
    return rtgh_desc_hid_report;
}

static uint16_t str_buf[128];

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;  // oryginał odpowiada tak samo niezależnie od LANGID
    if (rtgh_dump_mode) return dump_desc_string(index);
    if (index >= RTGH_STRING_COUNT || rtgh_strings[index] == NULL) {
        return NULL;  // STALL
    }
    uint8_t const *s = rtgh_strings[index];
    memcpy(str_buf, s, s[0]);  // surowy deskryptor: bLength, 0x03, UTF-16LE
    return str_buf;
}
