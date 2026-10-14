import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hidctl", ROOT / "tools/hidctl.py")
hidctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hidctl)


class FakePeer:
    def __init__(self):
        self.commands = []
        self.closed = False
        self.fail_press = False

    def command(self, code, payload=b""):
        self.commands.append((code, payload))
        if self.fail_press and code == 0x11 and payload[-1] == 1:
            raise hidctl.ProtocolError("Lost acknowledgement after press")
        return {"usb_connected": True, "keyboard_mode": 1, "mouse_mode": 0x10}

    def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def test_standard_crc_vector(self):
        self.assertEqual(hidctl.crc16(b"123456789"), 0x4B37)
        self.assertEqual(hidctl.request(1).hex(), "3301000000001838")

    def test_device_response_validation(self):
        body = bytes.fromhex("348091c10000")
        frame = body + struct.pack(">H", hidctl.crc16(body))
        self.assertTrue(hidctl.response(frame)["usb_connected"])
        corrupt = frame[:-1] + bytes((frame[-1] ^ 1,))
        with self.assertRaises(hidctl.ProtocolError):
            hidctl.response(corrupt)
        rejected = bytes.fromhex("344000000000")
        with self.assertRaises(hidctl.ProtocolError):
            hidctl.response(rejected + struct.pack(">H", hidctl.crc16(rejected)))

    def test_input_requires_explicit_arming_before_opening_port(self):
        for action in [["release"], ["key", "72"], ["move", "1", "0"]]:
            with self.subTest(action=action), self.assertRaises(SystemExit):
                hidctl.main(["--port", "unused", *action], peer_factory=lambda _: self.fail("Opened port before arming"))

    def test_read_only_status_sends_only_ping(self):
        peer = FakePeer()
        with contextlib.redirect_stdout(io.StringIO()):
            hidctl.main(["--port", "unused", "status"], peer_factory=lambda _: peer)
        self.assertEqual(peer.commands, [(1, b"")])
        self.assertTrue(peer.closed)

    def test_lost_press_acknowledgement_still_releases_key(self):
        peer = FakePeer()
        peer.fail_press = True
        with self.assertRaises(hidctl.ProtocolError):
            hidctl.main(["--port", "unused", "--allow-input", "key", "72"], peer_factory=lambda _: peer)
        self.assertEqual(peer.commands[-1], (0x11, b"\x48\x00"))
        self.assertTrue(peer.closed)

    def test_unsigned_packet_encoding_of_negative_motion(self):
        peer = FakePeer()
        with contextlib.redirect_stdout(io.StringIO()):
            hidctl.main(["--port", "unused", "--allow-input", "move", "-5", "3"], peer_factory=lambda _: peer)
        self.assertEqual(peer.commands[-1], (0x15, b"\xfb\x03"))

    def test_absolute_descriptor_has_its_own_report_id(self):
        # USB HID item parser verifies the actual firmware descriptor rather than
        # duplicating it: ID 3 has 48 input bits (buttons + X/Y + wheel).
        import re

        source = (ROOT / "src/main.cpp").read_text()
        array = source.split("abs_mouse_report_desc[] = {", 1)[1].split("};", 1)[0]
        array = re.sub(r"//[^\n]*", "", array)
        data = bytes(int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]+)", array))
        index = size = count = report_id = bits = 0
        while index < len(data):
            prefix = data[index]
            length = (0, 1, 2, 4)[prefix & 3]
            value = int.from_bytes(data[index + 1:index + 1 + length], "little")
            tag = prefix & 0xFC
            if tag == 0x84:
                report_id = value
            elif tag == 0x74:
                size = value
            elif tag == 0x94:
                count = value
            elif tag == 0x80:
                self.assertEqual(report_id, 3)
                bits += size * count
            index += length + 1
        self.assertEqual(bits, 48)


if __name__ == "__main__":
    unittest.main()
