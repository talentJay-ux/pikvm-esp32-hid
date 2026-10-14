#!/usr/bin/env python3
"""Drive a PiKVM-compatible UART HID; status is read-only by default."""

import argparse
import json
import struct
import time

try:
    import termios
    UART_ERRORS = (OSError, termios.error)
except ImportError:
    UART_ERRORS = (OSError,)


class ProtocolError(RuntimeError):
    pass


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def request(command, payload=b""):
    if not 0 <= command <= 255 or len(payload) > 4:
        raise ValueError("Command must be a byte and payload at most four bytes")
    body = bytes((0x33, command)) + payload.ljust(4, b"\0")
    return body + struct.pack(">H", crc16(body))


def response(frame):
    if len(frame) != 8 or frame[0] != 0x34:
        raise ProtocolError("Invalid response frame")
    if crc16(frame[:6]) != int.from_bytes(frame[6:], "big"):
        raise ProtocolError("Response CRC mismatch")
    if not frame[1] & 0x80:
        raise ProtocolError(f"Device rejected command: 0x{frame[1]:02x}")
    result = {
        "status": frame[1],
        "keyboard_mode": frame[2] & 7,
        "mouse_mode": frame[2] & 0x38,
        "usb_connected": bool(frame[3] & 0x40),
        "keyboard_online": not bool(frame[1] & 8),
        "mouse_online": not bool(frame[1] & 0x10),
    }
    if frame[4] & 0x80:
        result.update(usb_suspended=bool(frame[4] & 1), input_ready=bool(frame[4] & 2), keyboard_boot_protocol=bool(frame[4] & 4))
    return result


class Peer:
    def __init__(self, port):
        import serial

        self.serial = serial.Serial(port=None, baudrate=115200, timeout=0.1)
        # Avoid deliberately asserting reset. Some USB-UART drivers still
        # pulse the board while opening; let ROM/setup finish before PING.
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.port = port
        self.serial.open()
        time.sleep(0.75)

    def command(self, code, payload=b""):
        try:
            return self._command(code, payload)
        except UART_ERRORS as error:
            raise ProtocolError(f"Serial communication failed: {error}") from error

    def _command(self, code, payload=b""):
        self.serial.reset_input_buffer()
        self.serial.write(request(code, payload))
        self.serial.flush()
        deadline = time.monotonic() + 2
        buffer = bytearray()
        while time.monotonic() < deadline:
            buffer.extend(self.serial.read(8))
            while len(buffer) >= 8:
                if buffer[0] != 0x34:
                    del buffer[0]
                    continue
                frame = bytes(buffer[:8])
                if crc16(frame[:6]) != int.from_bytes(frame[6:], "big"):
                    del buffer[0]
                    continue
                return response(frame)
        raise ProtocolError("Timed out waiting for the HID controller")

    def close(self):
        self.serial.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--port", required=True)
    result.add_argument("--allow-input", action="store_true", help="Explicitly enable input commands; they affect the USB target's current focus")
    sub = result.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    sub.add_parser("release")
    key = sub.add_parser("key")
    key.add_argument("code", type=int, choices=range(1, 116), help="PiKVM MCU key code, not a USB usage number")
    key.add_argument("--hold-ms", type=int, default=50)
    move = sub.add_parser("move")
    move.add_argument("dx", type=int, choices=range(-127, 128))
    move.add_argument("dy", type=int, choices=range(-127, 128))
    return result


def main(argv=None, peer_factory=Peer):
    args = parser().parse_args(argv)
    if args.action != "status" and not args.allow_input:
        raise SystemExit("Input disabled: add --allow-input only when the target is ready")
    if args.action == "key" and not 0 <= args.hold_ms <= 1000:
        raise SystemExit("hold-ms must be between 0 and 1000")
    peer = peer_factory(args.port)
    try:
        if args.action == "status":
            result = peer.command(0x01)
        elif args.action == "release":
            result = peer.command(0x10)
        elif args.action == "move":
            state = peer.command(0x01)
            if not state["usb_connected"] or state["mouse_mode"] != 0x10:
                raise ProtocolError("Relative mouse requires a connected USB target and relative mode")
            result = peer.command(0x15, bytes((args.dx & 255, args.dy & 255)))
        else:
            state = peer.command(0x01)
            if not state["usb_connected"] or state["keyboard_mode"] != 1:
                raise ProtocolError("Keyboard requires a connected USB target and USB keyboard mode")
            try:
                peer.command(0x11, bytes((args.code, 1)))
                time.sleep(args.hold_ms / 1000)
            finally:
                result = peer.command(0x11, bytes((args.code, 0)))
        print(json.dumps(result))
    finally:
        peer.close()


if __name__ == "__main__":
    main()
