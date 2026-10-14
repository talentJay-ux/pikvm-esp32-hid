import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from hidweb import Controller
from hidctl import ProtocolError


class FakePeer:
    def __init__(self):
        self.commands = []

    def command(self, code, payload=b""):
        self.commands.append((code, payload))
        return {"usb_connected": True, "mouse_mode": 0x10, "keyboard_mode": 1}

    def close(self):
        pass


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.peer = FakePeer()
        self.controller = Controller(self.peer, clock=lambda: self.now)
        self.controller.poll()

    def test_default_polling_never_sends_input(self):
        self.controller.poll()
        self.assertEqual(self.peer.commands, [(1, b""), (1, b"")])
        with self.assertRaises(ValueError):
            self.controller.action("input", {"kind": "key", "code": 1, "down": True})

    def test_expired_browser_session_releases_held_key_despite_uart_pings(self):
        token = self.controller.action("arm", {})["token"]
        self.controller.action("input", {"token": token, "kind": "key", "code": 72, "down": True})
        self.now = 2
        self.controller.poll()
        self.assertEqual(self.peer.commands[-2:], [(0x10, b""), (1, b"")])
        self.assertFalse(self.controller.snapshot()["armed"])

    def test_only_one_controller_and_exact_token_allowed(self):
        self.controller.action("arm", {})
        with self.assertRaises(ValueError):
            self.controller.action("arm", {})
        with self.assertRaises(ValueError):
            self.controller.action("heartbeat", {"token": "wrong"})

    def test_invalid_motion_does_not_reach_device(self):
        token = self.controller.action("arm", {})["token"]
        with self.assertRaises(ValueError):
            self.controller.action("input", {"token": token, "kind": "move", "dx": 128, "dy": 0})
        self.assertEqual(self.peer.commands, [(1, b"")])

    def test_serial_failure_stops_polling_and_reconnects_without_lease(self):
        token = self.controller.action("arm", {})["token"]
        self.controller.action("input", {"token": token, "kind": "key", "code": 78, "down": True})
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
        self.assertFalse(self.controller.snapshot()["armed"])
        with self.assertRaises(ValueError):
            self.controller.action("heartbeat", {"token": token})

    def test_failed_input_ack_closes_port_and_cancels_control(self):
        token = self.controller.action("arm", {})["token"]
        def fail(*args):
            raise ProtocolError("Lost ACK")
        self.peer.command = fail
        with self.assertRaises(ProtocolError):
            self.controller.action("input", {"token": token, "kind": "key", "code": 78, "down": True})
        self.assertIsNone(self.controller.peer)
        self.assertFalse(self.controller.snapshot()["armed"])

    def test_browser_keymap_matches_real_firmware_usages(self):
        import json
        import re
        root = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
