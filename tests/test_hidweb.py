import io
import json
import os
import struct
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from hidweb import Controller, Video, WebSocket
from hidctl import ProtocolError


class FakePeer:
    def __init__(self, mouse_mode=0x08):
        self.commands = []
        self.mouse_mode = mouse_mode

    def command(self, code, payload=b""):
        self.commands.append((code, payload))
        return {"usb_connected": True, "mouse_mode": self.mouse_mode, "keyboard_mode": 1}

    def close(self):
        pass


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.peer = FakePeer()
        self.controller = Controller(self.peer, clock=lambda: self.now)
        self.controller.poll()
        self.owner = object()

    def test_default_polling_never_sends_input(self):
        self.controller.poll()
        self.assertEqual(self.peer.commands, [(1, b""), (1, b"")])
        with self.assertRaises(ValueError):
            self.controller.input(self.owner, {"kind": "key", "code": 1, "down": True})

    def test_silent_browser_releases_held_key_but_keeps_session(self):
        self.controller.acquire(self.owner)
        self.controller.input(self.owner, {"kind": "key", "code": 72, "down": True})
        self.now = 2
        self.controller.poll()
        self.assertEqual(self.peer.commands[-2:], [(0x10, b""), (1, b"")])
        self.assertTrue(self.controller.snapshot()["armed"])
        self.controller.input(self.owner, {"kind": "key", "code": 72, "down": False})

    def test_pings_keep_held_keys(self):
        self.controller.acquire(self.owner)
        self.controller.input(self.owner, {"kind": "key", "code": 72, "down": True})
        for step in range(1, 9):
            self.now = step * 0.5
            self.controller.input(self.owner, {"kind": "ping"})
            self.controller.poll()
        self.assertNotIn((0x10, b""), self.peer.commands)

    def test_only_one_controller_and_only_its_owner_may_send(self):
        self.controller.acquire(self.owner)
        with self.assertRaises(ValueError):
            self.controller.acquire(object())
        with self.assertRaises(ValueError):
            self.controller.input(object(), {"kind": "ping"})

    def test_release_clears_held_input_and_frees_control(self):
        self.controller.acquire(self.owner)
        self.controller.input(self.owner, {"kind": "button", "button": 0, "down": True})
        self.controller.release(self.owner)
        self.assertEqual(self.peer.commands[-1], (0x10, b""))
        self.assertFalse(self.controller.snapshot()["armed"])
        self.controller.acquire(object())

    def test_absolute_position_maps_full_range_to_firmware_int16(self):
        self.controller.acquire(self.owner)
        for x, y, expected in ((0, 0, (-32768, -32768)), (65535, 65535, (32767, 32767)), (32768, 16384, (0, -16384))):
            self.controller.input(self.owner, {"kind": "abs", "x": x, "y": y})
            self.assertEqual(self.peer.commands[-1], (0x12, struct.pack(">hh", *expected)))

    def test_invalid_or_wrong_mode_motion_does_not_reach_device(self):
        self.controller.acquire(self.owner)
        for data in ({"kind": "abs", "x": 65536, "y": 0}, {"kind": "abs", "x": 1.5, "y": 0}, {"kind": "move", "dx": 1, "dy": 0}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.controller.input(self.owner, data)
        relative = Controller(FakePeer(mouse_mode=0x10))
        relative.poll()
        relative.acquire(self.owner)
        with self.assertRaises(ValueError):
            relative.input(self.owner, {"kind": "abs", "x": 1, "y": 1})
        self.assertEqual(self.peer.commands, [(1, b"")])

    def test_serial_failure_stops_polling_and_reconnects_without_session(self):
        self.controller.acquire(self.owner)
        self.controller.input(self.owner, {"kind": "key", "code": 78, "down": True})
        replacement = FakePeer()
        self.controller.reconnect = lambda: replacement
        def fail(*args):
            raise ProtocolError("USB UART unplugged")
        self.peer.command = fail
        self.controller.poll()
        self.assertFalse(self.controller.snapshot()["armed"])
        self.assertTrue(self.controller.snapshot()["error"])
        self.now = 1.9
        self.controller.poll()
        self.assertEqual(replacement.commands, [])
        self.now = 2
        self.controller.poll()
        self.assertEqual(replacement.commands, [(1, b"")])
        with self.assertRaises(ValueError):
            self.controller.input(self.owner, {"kind": "ping"})

    def test_failed_input_ack_closes_port_and_cancels_control(self):
        self.controller.acquire(self.owner)
        def fail(*args):
            raise ProtocolError("Lost ACK")
        self.peer.command = fail
        with self.assertRaises(ProtocolError):
            self.controller.input(self.owner, {"kind": "key", "code": 78, "down": True})
        self.assertIsNone(self.controller.peer)
        self.assertFalse(self.controller.snapshot()["armed"])

    def test_browser_keymap_matches_real_firmware_usages(self):
        root = Path(__file__).resolve().parents[1]
        import re
        source = (root / "src/main.cpp").read_text().split("mcu_to_usb[116] = {", 1)[1].split("};", 1)[0]
        table = [int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]+)", source)]
        keys = json.loads((root / "tools/keymap.json").read_text())
        usages = {"ControlLeft": 0xE0, "ShiftLeft": 0xE1, "ArrowUp": 0x52, "ArrowDown": 0x51, "Enter": 0x28, "Backspace": 0x2A, "Tab": 0x2B, "Space": 0x2C, "CapsLock": 0x39, "Escape": 0x29}
        usages.update({"Key" + chr(65+i): 4+i for i in range(26)})
        usages.update({"Digit" + str(i): 0x1D+i for i in range(1,10)})
        usages.update({"F" + str(i): 0x39+i for i in range(1,13)})
        for name, usage in usages.items():
            with self.subTest(key=name):
                self.assertEqual(table[keys[name]], usage)


class VideoTests(unittest.TestCase):
    def test_new_frame_wakes_waiting_viewer(self):
        video = Video("/dev/null", 20, 6)
        video.publish(b"a")
        first = video.serial
        self.assertEqual(video.wait(first, 0), (first, b"a"))
        video.publish(b"b")
        self.assertEqual(video.wait(first, 0), (first + 1, b"b"))


def client_frame(opcode, payload):
    mask = os.urandom(4)
    length = bytes((0x80 | len(payload),)) if len(payload) < 126 else bytes((0x80 | 126,)) + struct.pack(">H", len(payload))
    return bytes((0x80 | opcode,)) + length + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebSocketTests(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        self.assertEqual(WebSocket.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_reads_masked_text_answers_ping_and_stops_at_close(self):
        long_text = "x" * 300
        incoming = io.BytesIO(client_frame(0x9, b"hi") + client_frame(0x1, b'{"kind":"ping"}') + client_frame(0x1, long_text.encode()) + client_frame(0x8, b""))
        outgoing = io.BytesIO()
        socket = WebSocket(incoming, outgoing)
        self.assertEqual(socket.receive(), '{"kind":"ping"}')
        self.assertEqual(outgoing.getvalue(), b"\x8a\x02hi")
        self.assertEqual(socket.receive(), long_text)
        self.assertIsNone(socket.receive())

    def test_rejects_unmasked_and_truncated_frames(self):
        with self.assertRaises(ConnectionError):
            WebSocket(io.BytesIO(b"\x81\x02hi"), io.BytesIO()).receive()
        with self.assertRaises(ConnectionError):
            WebSocket(io.BytesIO(client_frame(0x1, b"hello")[:-2]), io.BytesIO()).receive()

    def test_server_frames_are_unmasked_json(self):
        outgoing = io.BytesIO()
        WebSocket(io.BytesIO(), outgoing).send_json({"type": "armed"})
        data = outgoing.getvalue()
        self.assertEqual(data[0], 0x81)
        self.assertEqual(json.loads(data[2:]), {"type": "armed"})


if __name__ == "__main__":
    unittest.main()
