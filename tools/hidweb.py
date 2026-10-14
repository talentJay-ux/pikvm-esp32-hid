#!/usr/bin/env python3
"""Local HDMI viewer and explicitly requested UART HID control over WebSocket."""

import argparse
import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

from hidctl import Peer, ProtocolError

MOUSE_ABS = 0x08
# Held keys must not outlive a silent network: release them when the browser
# has sent nothing (not even a ping) for this long, but keep the session.
IDLE_RELEASE_S = 2.0


class Controller:
    def __init__(self, peer, clock=time.monotonic, reconnect=None):
        self.peer = peer
        self.reconnect = reconnect
        self.retry_at = 0
        self.clock = clock
        self.lock = threading.Lock()
        self.owner = None
        self.last_seen = 0
        self.had_input = False
        self.state = {}
        self.error = None

    def _release_held(self):
        if self.had_input and self.peer is not None:
            self.peer.command(0x10)
        self.had_input = False

    def _offline(self, error):
        # Stop PINGs for at least the firmware's 1.5s dead-man interval.
        # Reopening a serial descriptor must never resume a browser session.
        self.error = str(error)
        self.owner = None
        self.had_input = False
        self.state = {}
        if self.peer is not None:
            try:
                self.peer.close()
            except OSError:
                pass
        self.peer = None
        self.retry_at = self.clock() + 2

    def poll(self):
        with self.lock:
            try:
                if self.peer is None:
                    if self.reconnect is None or self.clock() < self.retry_at:
                        return
                    self.peer = self.reconnect()
                if self.owner and self.had_input and self.clock() - self.last_seen >= IDLE_RELEASE_S:
                    self._release_held()
                self.state = self.peer.command(1)
                if not self.state["usb_connected"] and self.owner:
                    self._release_held()
                    self.owner = None
                self.error = None
            except (OSError, ProtocolError) as error:
                self._offline(error)

    def snapshot(self):
        with self.lock:
            return {**self.state, "armed": self.owner is not None, "error": self.error}

    def acquire(self, owner):
        with self.lock:
            if self.owner is not None:
                raise ValueError("Another control session is active")
            if self.error or not self.state.get("usb_connected"):
                raise ValueError("USB target is not ready")
            self.owner = owner
            self.last_seen = self.clock()
            self.had_input = False

    def release(self, owner):
        with self.lock:
            if self.owner is not owner:
                return
            try:
                self._release_held()
            except (OSError, ProtocolError) as error:
                self._offline(error)
            finally:
                self.owner = None

    def input(self, owner, data):
        with self.lock:
            if self.owner is not owner:
                raise ValueError("Control is not active")
            self.last_seen = self.clock()
            kind = data.get("kind")
            if kind == "ping":
                return None
            if kind == "key":
                code, down = data.get("code"), data.get("down")
                if type(code) is not int or not 1 <= code <= 115 or type(down) is not bool:
                    raise ValueError("Invalid key")
                command, payload = 0x11, bytes((code, int(down)))
            elif kind == "abs":
                x, y = data.get("x"), data.get("y")
                if any(type(value) is not int or not 0 <= value <= 65535 for value in (x, y)):
                    raise ValueError("Invalid position")
                if self.state.get("mouse_mode") != MOUSE_ABS:
                    raise ValueError("Absolute mouse mode is required")
                command, payload = 0x12, struct.pack(">hh", x - 32768, y - 32768)
            elif kind == "button":
                button, down = data.get("button"), data.get("down")
                if type(button) is not int or button not in (0, 1, 2) or type(down) is not bool:
                    raise ValueError("Invalid button")
                select, pressed = {0: (0x80, 8), 1: (0x20, 2), 2: (0x40, 4)}[button]
                command, payload = 0x13, bytes((select | (pressed if down else 0), 0))
            elif kind == "wheel":
                dy = data.get("dy")
                if type(dy) is not int or not -127 <= dy <= 127:
                    raise ValueError("Invalid wheel movement")
                command, payload = 0x14, bytes((0, dy & 255))
            else:
                raise ValueError("Invalid input kind")
            self.had_input = True
            try:
                return self.peer.command(command, payload)
            except (OSError, ProtocolError) as error:
                self._offline(error)
                raise


class Video:
    def __init__(self, device, fps, quality):
        self.device = device
        self.fps = fps
        self.quality = quality
        self.frame = None
        self.serial = 0
        self.updated = 0
        self.changed = threading.Condition()

    def publish(self, frame):
        with self.changed:
            self.updated = time.monotonic()
            self.frame = frame
            self.serial += 1
            self.changed.notify_all()

    def wait(self, after, timeout):
        with self.changed:
            self.changed.wait_for(lambda: self.serial != after, timeout)
            return self.serial, self.frame

    def run(self):
        while True:
            # mpdecimate drops frames that differ only by capture noise but
            # keeps any frame with one clearly changed 8x8 block (a moved
            # pointer); max= still emits a frame every 2s so a static screen
            # is distinguishable from a dead capture. passthrough stops the
            # pipe muxer from re-duplicating dropped frames to a constant rate.
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1280x720", "-framerate", "30", "-i", self.device, "-vf", f"setpts=PTS-STARTPTS,fps={self.fps},mpdecimate=max={2 * self.fps}", "-fps_mode", "passthrough", "-c:v", "mjpeg", "-q:v", str(self.quality), "-f", "image2pipe", "-"]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                buffer = bytearray()
                while chunk := process.stdout.read(4096):
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9", max(0, start))
                        if start < 0 or end < 0:
                            break
                        self.publish(bytes(buffer[start:end + 2]))
                        del buffer[:end + 2]
                    if len(buffer) > 4_000_000:
                        buffer.clear()
            time.sleep(1)


class WebSocket:
    """Minimal RFC 6455 server side: text, ping and close frames only."""

    GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self.write_lock = threading.Lock()

    @classmethod
    def accept_key(cls, key):
        return base64.b64encode(hashlib.sha1(key.encode() + cls.GUID).digest()).decode()

    def _read_exact(self, count):
        data = self.rfile.read(count)
        if len(data) != count:
            raise ConnectionError("WebSocket closed")
        return data

    def receive(self):
        """Return the next text message, or None when the peer closes."""
        while True:
            first, second = self._read_exact(2)
            opcode, length = first & 0x0F, second & 0x7F
            if not second & 0x80:
                raise ConnectionError("Client frames must be masked")
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                raise ConnectionError("Frame too large")
            if length > 4096 or not first & 0x80:
                raise ConnectionError("Fragmented or oversized frame")
            mask = self._read_exact(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode == 0x1:
                return payload.decode()

    def _send(self, opcode, payload):
        header = bytes((0x80 | opcode,)) + (bytes((len(payload),)) if len(payload) < 126 else bytes((126,)) + struct.pack(">H", len(payload)))
        with self.write_lock:
            self.wfile.write(header + payload)
            self.wfile.flush()

    def send_json(self, value):
        self._send(0x1, json.dumps(value).encode())


def run(args):
    controller = Controller(None, reconnect=lambda: Peer(args.port))
    controller.poll()
    video = Video(args.video, args.fps, args.quality)
    threading.Thread(target=video.run, daemon=True).start()

    def poll():
        while True:
            controller.poll()
            time.sleep(0.3)

    threading.Thread(target=poll, daemon=True).start()

    def status():
        state = controller.snapshot()
        state["video_ready"] = time.monotonic() - video.updated < 3
        state["max_fps"] = args.fps
        return state

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def send(self, code, body, kind="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def same_origin(self):
            origin = self.headers.get("Origin")
            return not origin or origin == "http://" + self.headers.get("Host", "")

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/":
                self.send(200, Path(__file__).with_name("control.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/status":
                self.send(200, json.dumps(status()).encode())
            elif url.path == "/keymap":
                self.send(200, Path(__file__).with_name("keymap.json").read_bytes())
            elif url.path == "/pointer.mjs":
                self.send(200, Path(__file__).with_name("pointer.mjs").read_bytes(), "text/javascript")
            elif url.path == "/frame.jpg":
                self.send(200 if video.frame else 503, video.frame or b"", "image/jpeg")
            elif url.path == "/stream":
                self.stream(url)
            elif url.path == "/ws":
                self.control()
            else:
                self.send(404, b"{}")

        def stream(self, url):
            try:
                fps = min(args.fps, max(1, int(parse_qs(url.query).get("fps", [args.fps])[0])))
            except ValueError:
                self.send(400, b'{"error":"fps must be an integer"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = None
            try:
                while True:
                    started = time.monotonic()
                    seen, frame = video.wait(seen, 5)
                    if frame and time.monotonic() - video.updated < 3:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                        self.wfile.flush()
                    time.sleep(max(0, 1 / fps - (time.monotonic() - started)))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def control(self):
            key = self.headers.get("Sec-WebSocket-Key")
            if not self.same_origin():
                self.send(403, b'{"error":"Origin denied"}')
                return
            if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                self.send(400, b'{"error":"WebSocket upgrade required"}')
                return
            # Browsers reject an HTTP/1.0 status line for the upgrade, and
            # the base handler speaks HTTP/1.0, so write the 101 by hand.
            self.close_connection = True
            self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + WebSocket.accept_key(key) + "\r\n\r\n").encode())
            self.wfile.flush()
            socket = WebSocket(self.rfile, self.wfile)
            owner = object()
            try:
                controller.acquire(owner)
            except ValueError as error:
                socket.send_json({"type": "error", "error": str(error)})
                return
            socket.send_json({"type": "armed", "status": status()})
            try:
                while (message := socket.receive()) is not None:
                    try:
                        data = json.loads(message)
                        if not isinstance(data, dict):
                            raise ValueError("JSON object required")
                        controller.input(owner, data)
                    except (ValueError, ProtocolError, OSError) as error:
                        socket.send_json({"type": "error", "error": str(error)})
                        if controller.snapshot()["armed"] is False:
                            return
            except (ConnectionError, OSError):
                pass
            finally:
                controller.release(owner)

    server = ThreadingHTTPServer((args.listen, args.http_port), Handler)
    server.daemon_threads = True

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        with controller.lock:
            if controller.peer is not None:
                try:
                    controller._release_held()
                finally:
                    controller.peer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=9040)
    parser.add_argument("--fps", type=int, default=20, choices=range(1, 31), metavar="1-30", help="Highest frame rate a viewer may request")
    parser.add_argument("--quality", type=int, default=6, choices=range(2, 32), metavar="2-31", help="ffmpeg MJPEG -q:v; higher is smaller and blurrier")
    run(parser.parse_args())
