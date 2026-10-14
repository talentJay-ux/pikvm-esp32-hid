#!/usr/bin/env python3
"""Local HDMI viewer and explicitly armed UART HID control."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import signal
import threading
import time
import uuid

from hidctl import Peer, ProtocolError


class Controller:
    def __init__(self, peer, clock=time.monotonic, reconnect=None):
        self.peer = peer
        self.reconnect = reconnect
        self.retry_at = 0
        self.clock = clock
        self.lock = threading.Lock()
        self.token = None
        self.expiry = 0
        self.had_input = False
        self.state = {}
        self.error = None

    def _disarm(self):
        try:
            if self.had_input:
                self.peer.command(0x10)
        finally:
            self.token = None
            self.had_input = False

    def _offline(self, error):
        # Stop PINGs for at least the firmware's 1.5s dead-man interval.
        # Reopening a serial descriptor must never resume a browser lease.
        self.error = str(error)
        self.token = None
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
                if self.token and self.clock() >= self.expiry:
                    self._disarm()
                self.state = self.peer.command(1)
                if not self.state["usb_connected"] and self.token:
                    self._disarm()
                self.error = None
            except (OSError, ProtocolError) as error:
                self._offline(error)

    def snapshot(self):
        with self.lock:
            return {**self.state, "armed": bool(self.token), "error": self.error}

    def action(self, action, data):
        with self.lock:
            if action == "arm":
                if self.token and self.clock() < self.expiry:
                    raise ValueError("Another control session is active")
                if self.token:
                    self._disarm()
                if self.error or not self.state.get("usb_connected"):
                    raise ValueError("USB target is not ready")
                self.token = uuid.uuid4().hex
                self.expiry = self.clock() + 1.5
                return {"token": self.token}
            if not self.token or data.get("token") != self.token:
                raise ValueError("Control is not armed")
            if self.clock() >= self.expiry:
                self._disarm()
                raise ValueError("Control session expired")
            if action == "pause":
                self._disarm()
                return {"armed": False}
            if action == "heartbeat":
                self.expiry = self.clock() + 1.5
                return {"armed": True}
            kind = data.get("kind")
            if kind == "key":
                code, down = data.get("code"), data.get("down")
                if type(code) is not int or not 1 <= code <= 115 or type(down) is not bool:
                    raise ValueError("Invalid key")
                command, payload = 0x11, bytes((code, int(down)))
            elif kind == "move":
                dx, dy = data.get("dx"), data.get("dy")
                if any(type(value) is not int or not -127 <= value <= 127 for value in (dx, dy)):
                    raise ValueError("Invalid movement")
                if self.state.get("mouse_mode") != 0x10:
                    raise ValueError("Relative mouse mode is required")
                command, payload = 0x15, bytes((dx & 255, dy & 255))
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
                result = self.peer.command(command, payload)
            except (OSError, ProtocolError) as error:
                self._offline(error)
                raise
            self.expiry = self.clock() + 1.5
            return result


class Video:
    def __init__(self, device):
        self.device = device
        self.frame = None
        self.updated = 0

    def run(self):
        while True:
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1280x720", "-framerate", "30", "-i", self.device, "-vf", "setpts=PTS-STARTPTS,fps=5", "-c:v", "mjpeg", "-q:v", "5", "-f", "image2pipe", "-"]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                buffer = bytearray()
                while chunk := process.stdout.read(4096):
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9", max(0, start))
                        if start < 0 or end < 0:
                            break
                        self.frame = bytes(buffer[start:end + 2])
                        self.updated = time.monotonic()
                        del buffer[:end + 2]
                    if len(buffer) > 4_000_000:
                        buffer.clear()
            time.sleep(1)


def run(args):
    controller = Controller(None, reconnect=lambda: Peer(args.port))
    controller.poll()
    video = Video(args.video)
    threading.Thread(target=video.run, daemon=True).start()

    def poll():
        while True:
            controller.poll()
            time.sleep(0.3)

    threading.Thread(target=poll, daemon=True).start()

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

        def do_GET(self):
            if self.path == "/":
                self.send(200, Path(__file__).with_name("control.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/status":
                state = controller.snapshot()
                state["video_ready"] = time.monotonic() - video.updated < 3
                self.send(200, json.dumps(state).encode())
            elif self.path == "/keymap":
                self.send(200, Path(__file__).with_name("keymap.json").read_bytes())
            elif self.path == "/frame.jpg":
                self.send(200 if video.frame else 503, video.frame or b"", "image/jpeg")
            elif self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while True:
                        frame = video.frame
                        if frame and time.monotonic() - video.updated < 3:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                            self.wfile.flush()
                        time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send(404, b"{}")

        def do_POST(self):
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.send(403, b'{"error":"Origin denied"}')
                return
            try:
                if self.path not in ("/arm", "/pause", "/heartbeat", "/input"):
                    raise ValueError("Unknown action")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096 or self.headers.get_content_type() != "application/json":
                    raise ValueError("JSON request required")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                result = controller.action(self.path[1:], data)
                self.send(200, json.dumps(result).encode())
            except (ValueError, ProtocolError, OSError) as error:
                self.send(409, json.dumps({"error": str(error)}).encode())

    server = ThreadingHTTPServer((args.listen, args.http_port), Handler)
    server.daemon_threads = True
    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        with controller.lock:
            controller._disarm()
        if controller.peer is not None:
            controller.peer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=9040)
    run(parser.parse_args())
