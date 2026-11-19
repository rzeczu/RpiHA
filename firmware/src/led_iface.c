// Interfejs 1 "RT-GH LED" (klasa 0xFF, endpointy interrupt 0x82 IN, 0x03 OUT, 0x84 IN).
// Emulator (main.c): przyjmujemy wszystko, co host wysyła, logujemy, na IN nic nie wysyłamy (NAK).
// Proxy (proxy_main.c): przez haki dane idą do/z oryginalnej gitary.
#include <stdio.h>
#include <string.h>
#include "tusb.h"
#include "device/usbd_pvt.h"
#include "hardware/structs/usb.h"
#include "usblog.h"
#include "led_iface.h"

volatile uint32_t rtgh_led_packets;  // licznik dla main.c (miganie diodą)

static uint8_t led_rhport;
static uint8_t led_itf = 0xFF;
static uint8_t led_ep_out;
static uint8_t led_ep_in[2];
static uint8_t led_buf[64];
static uint8_t led_last[64];
static uint16_t led_last_len = 0xFFFF;
static uint8_t in_buf[2][64];
static uint8_t ctrl_buf[64];

__attribute__((weak)) void rtgh_led_out_hook(uint8_t const *d, uint16_t n) { (void)d; (void)n; }
__attribute__((weak)) void rtgh_led_in_done_hook(uint8_t ep) { (void)ep; }
__attribute__((weak)) int rtgh_ctrl_hook(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req,
                                         uint8_t *buf, uint16_t buflen) {
    (void)rhport; (void)stage; (void)req; (void)buf; (void)buflen;
    return -1;
}

static void dump(const char *what, uint8_t const *d, uint32_t n) {
    printf("%s (%lu B):", what, (unsigned long)n);
    for (uint32_t i = 0; i < n; i++) printf(" %02x", d[i]);
    printf("\n");
}

static int in_index(uint8_t ep) {
    for (int i = 0; i < 2; i++)
        if (led_ep_in[i] && led_ep_in[i] == ep) return i;
    return -1;
}

bool led_iface_in_busy(uint8_t ep) {
    if (led_itf == 0xFF || in_index(ep) < 0) return true;
    return usbd_edpt_busy(led_rhport, ep);
}

bool led_iface_in_send(uint8_t ep, uint8_t const *d, uint16_t n) {
    int i = in_index(ep);
    if (led_itf == 0xFF || i < 0 || !tud_mounted()) return false;
    if (n > sizeof(in_buf[i])) n = sizeof(in_buf[i]);
    if (!usbd_edpt_claim(led_rhport, ep)) return false;
    memcpy(in_buf[i], d, n);
    if (!usbd_edpt_xfer(led_rhport, ep, in_buf[i], n)) {
        usbd_edpt_release(led_rhport, ep);
        return false;
    }
    return true;
}

static void led_init(void) {}

static void led_reset(uint8_t rhport) {
    (void)rhport;
    led_itf = 0xFF;
    led_ep_out = 0;
    led_ep_in[0] = led_ep_in[1] = 0;
    led_last_len = 0xFFFF;
}

static uint16_t led_open(uint8_t rhport, tusb_desc_interface_t const *itf, uint16_t max_len) {
    if (itf->bInterfaceClass != TUSB_CLASS_VENDOR_SPECIFIC) return 0;
    uint8_t const *p = (uint8_t const *)itf;
    uint8_t const *end = p + max_len;
    uint16_t len = tu_desc_len(p);
    p = tu_desc_next(p);
    uint8_t found = 0, n_in = 0;
    led_rhport = rhport;
    while (found < itf->bNumEndpoints && p < end) {
        if (tu_desc_type(p) == TUSB_DESC_ENDPOINT) {
            tusb_desc_endpoint_t const *ep = (tusb_desc_endpoint_t const *)p;
            TU_ASSERT(usbd_edpt_open(rhport, ep), 0);
            uint8_t num = tu_edpt_number(ep->bEndpointAddress);
            if (tu_edpt_dir(ep->bEndpointAddress) == TUSB_DIR_IN && num > 0) {
                if (n_in < 2) led_ep_in[n_in++] = ep->bEndpointAddress;
                // flaga NAK w ep_nak_stall_status pokaże, czy host odpytuje IN, na które nic nie mamy
                usb_dpram->ep_ctrl[num - 1].in |= EP_CTRL_INTERRUPT_ON_NAK;
            }
            if (tu_edpt_dir(ep->bEndpointAddress) == TUSB_DIR_OUT) {
                led_ep_out = ep->bEndpointAddress;
                usbd_edpt_xfer(rhport, led_ep_out, led_buf, sizeof(led_buf));
            }
            found++;
        }
        len += tu_desc_len(p);
        p = tu_desc_next(p);
    }
    led_itf = itf->bInterfaceNumber;
    printf("LED: interfejs %u otwarty, OUT ep 0x%02x\n", led_itf, led_ep_out);
    usblog_text("LED interface opened");
    return len;
}

static bool ctrl_cb(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req) {
    int h = rtgh_ctrl_hook(rhport, stage, req, ctrl_buf, sizeof(ctrl_buf));
    if (h >= 0) return h != 0;
    if (stage == CONTROL_STAGE_SETUP) {
        printf("CTRL bm=%02x req=%02x wValue=%04x wIndex=%04x wLength=%u\n", req->bmRequestType,
               req->bRequest, req->wValue, req->wIndex, req->wLength);
        if (req->bmRequestType_bit.direction == TUSB_DIR_OUT) {
            return tud_control_xfer(rhport, req, ctrl_buf, tu_min16(req->wLength, sizeof(ctrl_buf)));
        }
        printf("CTRL IN -> STALL (nie znamy odpowiedzi oryginału)\n");
        return false;
    }
    if (stage == CONTROL_STAGE_DATA && req->bmRequestType_bit.direction == TUSB_DIR_OUT) {
        uint16_t n = tu_min16(req->wLength, sizeof(ctrl_buf));
        dump("CTRL dane", ctrl_buf, n);
        usblog_add(LOG_CTRL_OUT_DATA, ctrl_buf, (uint8_t)n);
    }
    return true;
}

static bool led_control_xfer_cb(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req) {
    return ctrl_cb(rhport, stage, req);
}

// żądania vendor skierowane do całego urządzenia
bool tud_vendor_control_xfer_cb(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req) {
    return ctrl_cb(rhport, stage, req);
}

static bool led_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result, uint32_t n) {
    (void)result;
    if (in_index(ep_addr) >= 0) {
        rtgh_led_in_done_hook(ep_addr);
        return true;
    }
    if (ep_addr != led_ep_out) return true;
    rtgh_led_packets++;
    if (n != led_last_len || memcmp(led_buf, led_last, n) != 0) {  // loguj tylko zmiany
        dump("LED OUT", led_buf, n);
        usblog_add(LOG_LED_OUT, led_buf, (uint8_t)n);
        memcpy(led_last, led_buf, n);
        led_last_len = (uint16_t)n;
    }
    rtgh_led_out_hook(led_buf, (uint16_t)n);
    usbd_edpt_xfer(rhport, led_ep_out, led_buf, sizeof(led_buf));
    return true;
}

static usbd_class_driver_t const led_driver = {
    .init = led_init,
    .reset = led_reset,
    .open = led_open,
    .control_xfer_cb = led_control_xfer_cb,
    .xfer_cb = led_xfer_cb,
    .sof = NULL,
};

usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count) {
    *driver_count = 1;
    return &led_driver;
}
