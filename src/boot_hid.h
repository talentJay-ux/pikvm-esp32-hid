#pragma once
#include "esp32-hal-tinyusb.h"
#include "hid_protocol.h"

struct KeyReport { uint8_t modifiers, reserved, keys[6]; };
static_assert(sizeof(KeyReport) == 8, "USB boot keyboard packet size");

namespace BootHid {
static const uint8_t keyboard_desc[] = {TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(1))};
static const uint8_t mouse_desc[] = {TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(2))};
static uint8_t descriptor[sizeof(keyboard_desc) + sizeof(abs_mouse_report_desc) + sizeof(mouse_desc)];
static KeyReport keyboard = {};
static volatile uint8_t leds = 0;

static bool boot() { return tud_hid_get_protocol() == HID_PROTOCOL_BOOT; }
static bool send(uint8_t id, const void *data, uint16_t len) {
    const unsigned long started = millis();
    while (!tud_hid_ready()) {
        if (!tud_mounted() || tud_suspended() || millis() - started >= 100) return false;
        delay(1);
    }
    return tud_hid_report(id, data, len);
}
static uint16_t load_descriptor(uint8_t *dst, uint8_t *itf) {
    const uint8_t ep = tinyusb_get_free_in_endpoint();
    if (!ep) return 0;
    const uint8_t desc[] = {
        TUD_HID_DESCRIPTOR(*itf, tinyusb_add_string_descriptor("PiKVM Boot Keyboard"),
                           HID_ITF_PROTOCOL_KEYBOARD, sizeof(descriptor), (uint8_t)(0x80 | ep), 64, 1)
    };
    memcpy(dst, desc, sizeof(desc));
    ++*itf;
    return sizeof(desc);
}
static bool begin() {
    memcpy(descriptor, keyboard_desc, sizeof(keyboard_desc));
    memcpy(descriptor + sizeof(keyboard_desc), abs_mouse_report_desc, sizeof(abs_mouse_report_desc));
    memcpy(descriptor + sizeof(keyboard_desc) + sizeof(abs_mouse_report_desc), mouse_desc, sizeof(mouse_desc));
    return tinyusb_enable_interface(USB_INTERFACE_CUSTOM, TUD_HID_DESC_LEN, load_descriptor) == ESP_OK;
}
}

extern "C" const uint8_t *tud_hid_descriptor_report_cb(uint8_t) { return BootHid::descriptor; }
extern "C" uint16_t tud_hid_get_report_cb(uint8_t, uint8_t id, hid_report_type_t type, uint8_t *buffer, uint16_t len) {
    if (type != HID_REPORT_TYPE_INPUT || id != keyboard_report_id(BootHid::boot())) return 0;
    const uint16_t size = min(len, (uint16_t)sizeof(BootHid::keyboard));
    memcpy(buffer, &BootHid::keyboard, size);
    return size;
}
extern "C" void tud_hid_set_report_cb(uint8_t, uint8_t id, hid_report_type_t type, const uint8_t *data, uint16_t len) {
    if (type == HID_REPORT_TYPE_OUTPUT && len && id == keyboard_report_id(BootHid::boot())) BootHid::leds = data[0];
}

class BootKeyboard {
public:
    bool sendReport(const KeyReport *report) {
        BootHid::keyboard = *report;
        return BootHid::send(keyboard_report_id(BootHid::boot()), report, sizeof(*report));
    }
};
class BootRelativeMouse {
public:
    bool send(uint8_t buttons, int8_t x, int8_t y, int8_t wheel) {
        if (!mouse_report_supported(BootHid::boot())) return false;
        const uint8_t report[] = {buttons, (uint8_t)x, (uint8_t)y, (uint8_t)wheel, 0};
        return BootHid::send(2, report, sizeof(report));
    }
};
class AbsMouse {
public:
    bool send(uint8_t buttons, uint16_t x, uint16_t y, int8_t wheel) {
        if (!mouse_report_supported(BootHid::boot())) return false;
        const uint8_t report[] = {buttons, (uint8_t)x, (uint8_t)(x >> 8), (uint8_t)y, (uint8_t)(y >> 8), (uint8_t)wheel};
        return BootHid::send(3, report, sizeof(report));
    }
};
