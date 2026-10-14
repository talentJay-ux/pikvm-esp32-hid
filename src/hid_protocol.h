#pragma once
#include <stdint.h>

// Boot keyboard packets have no report-ID prefix. Report mode keeps all three
// collections, while a host selecting keyboard boot mode cannot consume mice.
constexpr uint8_t keyboard_report_id(bool boot) { return boot ? 0 : 1; }
constexpr bool mouse_report_supported(bool boot) { return !boot; }
