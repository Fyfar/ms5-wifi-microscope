#!/usr/bin/env python3
# probe_resolution.py - Investigate (and optionally change) the MS5's capture
# resolution, WITHOUT touching ms5_viewer.py.
#
# Background (reverse-engineered from libWifiCamera.so, arm64):
#   * GetCameraConfig = command type 0x0d on UDP :10005, header-only (no payload).
#       response payload: [0]=format u8, [1:3]=width u16 LE, [3:5]=height u16 LE,
#                         [5]=count, then count*{ u8 format, u16 width, u16 height }.
#   * SetCameraConfig = command type 0x0e on UDP :10005, payload 5 bytes:
#       [0]=format u8, [1:3]=width u16 LE, [3:5]=height u16 LE.
#       (the library writes the header `length` field as 0 but still appends the
#        5 payload bytes -- same quirk as OpenVideo.)
#
#   Run while connected to the camera WiFi AP:
#       python3 probe_resolution.py            # read-only: dump current + supported
#       python3 probe_resolution.py W H        # try to set WxH, then verify
#
# Stdlib only. Talks only to 192.168.1.1.

import socket
import struct
import sys
import time

CAM   = "192.168.1.1"
P_CMD = 10005
P_OV  = 10006

MAGIC = 0xffeeffee
T_GET_CFG = 0x0d
T_SET_CFG = 0x0e
T_OPEN_VIDEO = 0x04


def _msg(msg_type, mid, payload=b"", unk=1, length=None):
    # length defaults to real payload length; pass length=0 to reproduce the
    # library's "header says 0, bytes still appended" quirk used by Set/OpenVideo.
    if length is None:
        length = len(payload)
    return struct.pack("<IHHBBH", MAGIC, mid, msg_type, unk, 0, length) + payload


def _req(sock, port, msg_type, mid, payload=b"", length=None, retries=8, timeout=2):
    """Send a command and return the first valid echoed response, or None.

    The MS5 UDP service often drops the first packet after idle, so we retry.
    """
    for _ in range(retries):
        try:
            sock.sendto(_msg(msg_type, mid, payload, length=length), (CAM, port))
            sock.settimeout(timeout)
            resp, src = sock.recvfrom(4096)
            if src[0] == CAM and len(resp) >= 12 and resp[:4] == b"\xee\xff\xee\xff":
                return resp
        except socket.timeout:
            pass
        except OSError:
            break
        mid = (mid + 1) & 0xffff
    return None


def parse_camera_config(payload):
    """Parse a GetCameraConfig (type 0x0d) response payload.

    Returns dict: {format, width, height, modes: [(w, fmt, h), ...]}.
    Layout: [0]=format u8, [1:3]=width u16 LE, [3:5]=height u16 LE,
            [5]=count, then count * { u8 format, u16 width LE, u16 height LE }.
    """
    if len(payload) < 6:
        raise ValueError("config payload too short: %d bytes" % len(payload))
    fmt = payload[0]
    width, height = struct.unpack_from("<HH", payload, 1)
    count = payload[5]
    modes = []
    off = 6
    for _ in range(count):
        if off + 5 > len(payload):
            break
        f, w, h = struct.unpack("<BHH", payload[off:off + 5])
        modes.append((w, f, h))
        off += 5
    return {"format": fmt, "width": width, "height": height, "modes": modes}


def get_config(sock):
    resp = _req(sock, P_CMD, T_GET_CFG, 1, retries=10)
    if resp is None:
        return None
    # header is 12 bytes; the rest is the config payload
    return parse_camera_config(resp[12:])


def set_config(sock, fmt, width, height):
    """Send SetCameraConfig (type 0x0e). Reproduces the length=0 quirk.

    Wire payload the library sends is format(u8), width(u16 LE), height(u16 LE).
    """
    payload = struct.pack("<BHH", fmt, width, height)
    resp = _req(sock, P_CMD, T_SET_CFG, 1, payload=payload, length=0, retries=8)
    return resp


def peek_stream_dimensions(timeout=3.0):
    """Open the video stream briefly and read the width/height the camera actually
    sends in the 16-byte chunk header (bytes [12:14]=width, [14:16]=height).

    This is ground truth: it reflects what the sensor is really streaming, which
    is how we tell a real resolution change from one the camera merely ack'd.
    Returns (width, height) or None.
    """
    vid = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    vid.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    vid.bind(("0.0.0.0", 0))
    port = vid.getsockname()[1]
    ov = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _req(ov, P_OV, T_OPEN_VIDEO, 1, payload=struct.pack("<H", port), length=0,
         retries=6, timeout=1.5)
    vid.settimeout(timeout)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                data, src = vid.recvfrom(8192)
            except socket.timeout:
                break
            if src[0] == CAM and len(data) >= 16:
                w, h = struct.unpack_from("<HH", data, 12)
                return (w, h)
    finally:
        vid.close()
        ov.close()
    return None


# --------------------------------------------------------------------------- #
# Verification: trust the camera's own reported config.
#
# After Set, we re-read GetCameraConfig (the cameraWifiResolutionGet path) and
# treat the change as successful if the camera now reports the requested w/h.
# Caveat we accept here: this firmware *could* ack-and-report a new size while
# the sensor keeps streaming the old one. If you ever want the stronger,
# stream-level proof, peek_stream_dimensions() reads the true w/h straight from
# the live chunk header -- cross-check against it when in doubt.
def verify_change(sock, resp, requested):
    if resp is None:
        print("  no ack from camera (Set timed out)")
        return False
    # NOTE: this firmware always replies to SetCameraConfig with a bare 12-byte
    # header and NO payload, whether or not the mode was applied -- so the ack
    # shape tells us nothing. The header `err` byte (offset 9) is printed for
    # reference, but the only trustworthy success signal is the config read-back.
    print("  raw ack (%d bytes): %s" % (len(resp), resp.hex()))
    if len(resp) >= 12:
        _, rid, rtype, runk, rerr, rlen = struct.unpack_from("<IHHBBH", resp, 0)
        print("  ack header: id=%d type=0x%02x err=%d len=%d (header-only ack is normal)"
              % (rid, rtype, rerr, rlen))
    cfg = get_config(sock)
    if cfg is None:
        print("  could not re-read config after Set")
        return False
    now = (cfg["width"], cfg["height"])
    print("  camera now reports: %dx%d  -> %s"
          % (now[0], now[1], "APPLIED" if now == requested else "NOT applied (clamped/ignored)"))
    return now == requested


def main():
    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print("Querying camera config (read-only) ...")
    cfg = get_config(cmd)
    if cfg is None:
        print("ERROR: no response from %s:%d - on the camera WiFi?" % (CAM, P_CMD))
        sys.exit(1)
    print("Current: format=%d  %dx%d" % (cfg["format"], cfg["width"], cfg["height"]))
    if cfg["modes"]:
        print("Supported modes the camera advertises:")
        for w, f, h in cfg["modes"]:
            print("   %5dx%-5d (format %d)" % (w, h, f))
    else:
        print("Camera advertised NO alternate modes (count=0) -> resolution may be fixed.")

    if len(sys.argv) >= 3:
        want_w, want_h = int(sys.argv[1]), int(sys.argv[2])
        # Hardware note (verified on MS5 fw ver1221): this camera only supports
        # 1280x720 and 640x480, both at the same ~25 fps -- lowering resolution
        # gives NO fps gain. Worse, changing the mode at runtime can wedge the
        # video encoder (command channel still acks, but no frames arrive); a
        # physical power-cycle is the only recovery. Use only for investigation.
        print("\n*** WARNING: a runtime resolution change can wedge the video")
        print("    pipeline on this firmware. If the stream goes to 0x0 / 0 fps,")
        print("    power-cycle the camera to recover. ***")
        print("Requesting %dx%d ..." % (want_w, want_h))
        resp = set_config(cmd, cfg["format"], want_w, want_h)
        ok = verify_change(cmd, resp, (want_w, want_h))
        print("Change verified (camera-reported config):", ok)
        # Ground truth: what is the sensor ACTUALLY streaming now? This is the
        # only signal that can't be faked by an ack-and-ignore firmware -- the
        # decisive test when requesting a mode the camera never advertised.
        print("Peeking real stream dimensions (opening video briefly) ...")
        dims = peek_stream_dimensions()
        if dims is None:
            print("  no video frames arrived (encoder may be wedged -> power-cycle)")
        else:
            print("  stream is actually sending: %dx%d" % dims)


if __name__ == "__main__":
    main()
