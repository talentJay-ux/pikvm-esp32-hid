# Two USB cables, no GPIO wiring

Build `pio run -e esp32s3_usb_uart` for an ESP32-S3 devkit with a native USB/OTG port and a separate UART0 USB bridge. The GPIO16/17 UART1 build remains available as `esp32s3`.

| S3 connector | Connect to | Purpose |
|---|---|---|
| USB / OTG | Target Windows PC USB | USB keyboard and mouse |
| UART / USB-to-UART | Raspberry Pi USB | 115200 8N1 control packets and power |

Keep the Pi independently powered. Connect the target PC HDMI output to the capture card HDMI input and the capture card USB to the Pi. Do not connect GPIO power pins for this configuration.

This build exposes `PiKVM ESP32-S3 HID` (VID 1209, PID EDA2), defaults to relative mouse, and does not type or move the pointer on boot. UART0 contains protocol responses without debug logs. Held keys/buttons are released after 1.5 seconds without a valid packet, or when the USB target disconnects. Hold sessions must send periodic PING requests; malformed packets do not renew the timeout.

The HID interface advertises Boot Keyboard support. When BIOS selects boot protocol, keyboard packets contain eight bytes without a report-ID prefix; use Tab, arrows and Enter for BIOS navigation. Mouse controls are unavailable in keyboard boot protocol. Report protocol retains keyboard ID 1, relative mouse ID 2 and absolute mouse ID 3 for the operating system. Failed HID sends return an error instead of acknowledging unsent input. Status extensions expose suspension, endpoint readiness and boot protocol when supported by the firmware.

Opening some USB-UART bridges briefly resets the board, even with DTR/RTS deasserted. The host waits 750 ms for setup before its first command. Keep one service owning the serial port; do not run the CLI while the web service is active.

Install `pyserial` in a Pi virtual environment, identify the bridge under `/dev/serial/by-id/`, and run:

```sh
python tools/hidctl.py --port /dev/serial/by-id/YOUR_UART_BRIDGE status
```

Status sends only PING. Input commands require `--allow-input`; for example, a short left-shift pulse uses MCU key code 78:

```sh
python tools/hidctl.py --port /dev/serial/by-id/YOUR_UART_BRIDGE --allow-input key 78
python tools/hidctl.py --port /dev/serial/by-id/YOUR_UART_BRIDGE --allow-input move 5 0
```

An extended HDMI display shares the Windows login session and keyboard focus with the DP display. It does not isolate input. Verify HID events on an idle test host first; use only read-only status checks while someone is using the target PC.

Before flashing, keep a verified full Flash backup and record the board MAC. After flashing, the native USB serial device becomes a HID device. Use the UART bridge for subsequent programming, or hold BOOT and press RESET to re-enter native USB download mode.

## HDMI viewer and control service

The Pi needs `ffmpeg`, `pyserial`, and permission to access both devices. Start the viewer with stable device paths:

```sh
python tools/hidweb.py --port /dev/serial/by-id/YOUR_UART_BRIDGE --video /dev/v4l/by-id/YOUR_CAPTURE_CARD
```

Forward its loopback-only web port through SSH:

```sh
ssh -N -L 127.0.0.1:18940:127.0.0.1:9040 pi@YOUR_PI
```

Open `http://127.0.0.1:18940/`. Viewing sends only PING; press the control button explicitly to send input. Drag the picture like a touchpad to move the relative pointer; tap it to left-click, or use the left/right click buttons. This works without browser pointer locking. Dragging moves the pointer without holding a Windows mouse button. The window button sends Win+Shift+Right to move the active window to the next display. Pointer events are coalesced while requests are pending, preserving key and button ordering instead of accumulating stale movement.

Esc, tab hiding, or browser focus loss releases control. Browser leases expire after 1.5 seconds even while the Pi continues polling. If UART communication fails, polling stops for two seconds so firmware releases held input, then reconnects without restoring control. Capture restarts automatically when its device returns.

Install the provided user service template with your actual Python and device paths. Enable user lingering (`sudo loginctl enable-linger YOUR_USER`) and `systemctl --user enable --now jay-hid.service` so it starts before login and recovers from process failures. No public HTTP listener is needed. The two USB connections alone do not provide a physical power-button relay.
