#!/usr/bin/env python3
# ms5_viewer.py - Live viewer for the MS5 WiFi microscope.
#
# Part of: https://github.com/Fyfar/ms5-wifi-microscope   (MIT License)
# Full protocol documentation is in README.md.
#
# Reverse-engineered i4season/Suear protocol (libWifiCamera.so). Pulls the
# camera's 1280x720 MJPEG-over-UDP stream and re-serves it as MJPEG-over-HTTP
# so you can watch it in a browser, VLC, ffplay, or OBS.
#
#   Run (while connected to the camera's WiFi AP):
#       python3 ms5_viewer.py
#   Then:
#       Browser:     http://127.0.0.1:45100
#       VLC/ffplay:  http://127.0.0.1:45100/stream
#       Snapshot:    http://127.0.0.1:45100/snapshot
#
# Stdlib only, py3.7+. No internet needed (talks only to 192.168.1.1).

import http.server
import socket
import struct
import sys
import threading
import time

CAM       = "192.168.1.1"
P_CMD     = 10005          # command channel (UDP)
P_OV      = 10006          # OpenVideo / stream-init (UDP)
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 45100
CHUNK_HDR = 16             # bytes of stream-chunk header before JPEG payload


class MS5Client:
    def __init__(self):
        self.vid = None
        self.cmd = None
        self.ov = None
        self.port = None             # our video recv port (announced to camera)
        self.device = {}
        self.width = 0
        self.height = 0
        self.running = False
        self.frames = 0
        self.last_rx = 0.0
        self._cond = threading.Condition()
        self._latest = None
        self._seq = 0

    # ---- protocol helpers ----
    @staticmethod
    def _msg(msg_type, mid, payload=b"", unk=1, length=None):
        if length is None:
            length = len(payload)
        return struct.pack("<IHHBBH", 0xffeeffee, mid, msg_type, unk, 0, length) + payload

    def _req(self, sock, port, msg_type, mid, payload=b"", length=None, retries=8, timeout=2):
        for _ in range(retries):
            try:
                sock.sendto(self._msg(msg_type, mid, payload, length=length), (CAM, port))
                sock.settimeout(timeout)
                resp, src = sock.recvfrom(4096)
                if (src[0] == CAM and len(resp) >= 12
                        and resp[:4] == b"\xee\xff\xee\xff"):
                    return resp
            except socket.timeout:
                pass
            except OSError:
                break
            mid = (mid + 1) & 0xffff
        return None

    def connect(self):
        # video recv socket on an OS-assigned ephemeral port
        self.vid = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.vid.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.vid.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.vid.bind(("0.0.0.0", 0))
        self.port = self.vid.getsockname()[1]

        # command channel: GetDeviceInfo (also warms up the flaky-first-packet service)
        self.cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        resp = self._req(self.cmd, P_CMD, 0x0001, 1, retries=10)
        if resp and len(resp) >= 12 + 113:
            d = resp[12:]
            self.device = {
                "vendor":  d[1:33].split(b"\0")[0].decode("ascii", "replace"),
                "product": d[33:65].split(b"\0")[0].decode("ascii", "replace"),
                "fw":      d[65:81].split(b"\0")[0].decode("ascii", "replace"),
                "ssid":    d[81:113].split(b"\0")[0].decode("ascii", "replace"),
            }
        elif resp is None:
            raise IOError("No response from camera at %s:%d - are you on the camera WiFi?"
                          % (CAM, P_CMD))

        # OpenVideo carrying our recv port -> camera starts streaming to self.port
        self.ov = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._open_video()

    def _open_video(self, retries=6, timeout=2):
        self._req(self.ov, P_OV, 0x0004, 1, payload=struct.pack("<H", self.port),
                  length=0, retries=retries, timeout=timeout)

    # ---- receive / reassemble loop (runs in its own thread) ----
    def run(self):
        self.running = True
        self.last_rx = time.time()
        last_rearm = 0.0
        cur = None
        chunks = {}
        self.vid.settimeout(0.5)
        while self.running:
            try:
                data, src = self.vid.recvfrom(8192)
            except socket.timeout:
                # stall watchdog: nudge the camera if no data for a while
                now = time.time()
                if now - self.last_rx > 1.0 and now - last_rearm > 1.0:
                    self._open_video(retries=2, timeout=0.4)
                    last_rearm = now
                continue
            except OSError:
                break
            if src[0] != CAM or len(data) < CHUNK_HDR:
                continue
            self.last_rx = time.time()
            n_frame = data[2]
            idx = data[4]
            self.width = struct.unpack_from("<H", data, 12)[0]
            self.height = struct.unpack_from("<H", data, 14)[0]
            if cur is None:
                cur = n_frame
            if n_frame != cur:
                self._emit(chunks)
                chunks = {}
                cur = n_frame
            chunks[idx] = data[CHUNK_HDR:]

    def _emit(self, chunks):
        if not chunks:
            return
        jpg = b"".join(chunks[k] for k in sorted(chunks))
        if jpg[:2] != b"\xff\xd8":
            return
        e = jpg.rfind(b"\xff\xd9")
        if e == -1:
            return
        jpg = jpg[:e + 2]
        with self._cond:
            self._latest = jpg
            self._seq += 1
            self.frames += 1
            self._cond.notify_all()

    def get_frame(self, last_seq, timeout=2.0):
        with self._cond:
            if self._seq == last_seq or self._latest is None:
                self._cond.wait(timeout)
            return self._latest, self._seq


# ----------------------------- HTTP server -----------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    client = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._page()
        elif self.path == "/snapshot":
            self._snapshot()
        elif self.path == "/stream":
            self._stream()
        else:
            self.send_response(404)
            self.end_headers()

    def _page(self):
        d = self.client.device
        info = "%s %s  fw %s  (%s)" % (d.get("vendor", "?"), d.get("product", "MS5"),
                                       d.get("fw", "?"), d.get("ssid", "?"))
        html = (
            "<!doctype html><html><head><title>MS5 Microscope</title>"
            "<style>body{background:#111;color:#ddd;font-family:sans-serif;text-align:center;margin:0}"
            "img{max-width:100vw;max-height:90vh;background:#000}"
            "p{font-size:13px;color:#888}</style></head><body>"
            "<img src='/stream'><p>%s &mdash; %dx%d</p></body></html>"
            % (info, self.client.width, self.client.height)
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _snapshot(self):
        jpg, _ = self.client.get_frame(-1, timeout=3.0)
        if not jpg:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpg)

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        last = -1
        try:
            while True:
                jpg, last = self.client.get_frame(last, timeout=2.0)
                if jpg is None:
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpg)).encode("ascii"))
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def fps_reporter(client):
    last = 0
    while True:
        time.sleep(5)
        cur = client.frames
        sys.stderr.write("  [stream] %d frames total  (~%.1f fps)  %dx%d\n"
                         % (cur, (cur - last) / 5.0, client.width, client.height))
        last = cur


def main():
    print("Connecting to MS5 at %s ..." % CAM)
    client = MS5Client()
    try:
        client.connect()
    except IOError as e:
        print("ERROR: %s" % e)
        sys.exit(1)
    d = client.device
    print("Device: %s %s  fw=%s  ssid=%s" % (d.get("vendor", "?"), d.get("product", "?"),
                                             d.get("fw", "?"), d.get("ssid", "?")))
    print("Video recv port: %d" % client.port)

    threading.Thread(target=client.run, daemon=True).start()
    threading.Thread(target=fps_reporter, args=(client,), daemon=True).start()

    Handler.client = client
    httpd = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    url = "http://%s:%d" % (HTTP_HOST, HTTP_PORT)
    print("\n  Live view : %s" % url)
    print("  VLC/ffplay: %s/stream" % url)
    print("  Snapshot  : %s/snapshot" % url)
    print("\nPress Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        client.running = False


if __name__ == "__main__":
    main()
