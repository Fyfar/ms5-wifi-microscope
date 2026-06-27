# MS5 WiFi Microscope — open-source viewer & protocol

A clean, from-scratch client for the cheap **MS5 WiFi digital microscope** (and likely many similar
i4season-based WiFi cameras/otoscopes/endoscopes). It connects to the camera over WiFi and streams its
native **1280×720 MJPEG** video to your **browser, VLC, ffplay, or OBS** — at roughly **~21 fps**.

The official app (`DLscope`) is broken and barely manages **<2 fps**. This project gets the camera's full
frame rate, runs anywhere Python runs, and **documents the entire reverse-engineered WiFi protocol** so
other owners can build their own tools.

- ✅ Pure **Python 3 standard library** — no `pip install`, no native deps.
- ✅ Works fully **offline** (talks only to the camera at `192.168.1.1`).
- ✅ Live view in a browser, or any MJPEG-capable player (VLC/ffplay/OBS).
- ✅ Full **protocol documentation** below.

> Status: **working and confirmed on real hardware.** 1280×720 @ ~21 fps.

---

## Table of contents
- [Compatibility](#compatibility)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Endpoints](#endpoints)
- [How it works](#how-it-works)
- [Protocol reference](#protocol-reference)
- [Troubleshooting](#troubleshooting)
- [Roadmap / help wanted](#roadmap--help-wanted)
- [How it was reverse-engineered](#how-it-was-reverse-engineered)
- [Credits](#credits)
- [License](#license)
- [Disclaimer](#disclaimer)

---

## Compatibility

Confirmed on the **MS5** (a JieLi-based WiFi microscope; the camera identifies itself as vendor `MKL`,
product `MS5`, firmware `ver1221`; AP SSID `wifi_camera_MS5_XXXX`).

The MS5 drives its camera through the **i4season `libWifiCamera.so`** stack, which is shared by a large
family of cheap WiFi cameras, **otoscopes/ear-cleaners, and endoscopes** sold under many brands and
using the **`com.i4season.*`** mobile apps (e.g. the "Suear" ear-cleaner family). The protocol described
here — and very possibly this viewer with little or no change — should work for those devices too.
**If you try it on another device, please open an issue with the result** (and the `GetDeviceInfo`
output) so we can grow the compatibility list.

---

## Requirements

- Python **3.7+** (standard library only).
- A computer that can join the camera's WiFi access point.
- An MJPEG viewer if you don't want to use a browser: VLC, `ffplay`, OBS, etc. (optional).

---

## Quick start

1. Power on the microscope and **join its WiFi network** (`wifi_camera_MS5_XXXX`). Your machine gets a
   `192.168.1.x` address; the camera is `192.168.1.1`.
2. Run the viewer:
   ```bash
   python3 ms5_viewer.py
   ```
3. Open the stream:
   - **Browser:** http://127.0.0.1:45100
   - **VLC / ffplay / OBS:** `http://127.0.0.1:45100/stream`
   - **Single snapshot:** http://127.0.0.1:45100/snapshot

That's it. The camera has no internet uplink, so joining its AP will take your machine offline — that's
expected and fine; the viewer only needs the local link.

---

## Endpoints

| URL | Description |
|-----|-------------|
| `/` | HTML page with the live `<img>` stream + device info |
| `/stream` | `multipart/x-mixed-replace` MJPEG stream (use this in VLC/ffplay/OBS) |
| `/snapshot` | a single current JPEG frame |

The viewer prints a live fps readout while running.

---

## How it works

```
  MS5 camera (UDP)                 ms5_viewer.py                     you
  192.168.1.1                ┌───────────────────────┐
        │  GetDeviceInfo     │  receive thread        │
        │◀──────────────────▶│  - handshake           │
        │  OpenVideo(+port)  │  - reassemble JPEG      │   browser /
        │◀──────────────────▶│  - publish latest frame │   VLC / ffplay
        │   MJPEG chunks     │                         │◀─ HTTP MJPEG ──▶
        │ ──────────────────▶│  HTTP server (MJPEG)    │
        └────────────────────┴───────────────────────┘
```

A background thread performs the WiFi handshake and reassembles the camera's chunked JPEG stream into
whole frames; a small HTTP server re-serves those frames as MJPEG to any number of viewers.

---

## Protocol reference

Everything is **UDP**. The camera is `192.168.1.1`. There is a small contiguous port block:

| Port | Role |
|------|------|
| `:10005` | command channel (request → response) |
| `:10006` | "OpenVideo" / stream-init |
| `:10007` | camera→client control push (a `type 0x09` descriptor; not needed to receive video) |
| client-chosen ephemeral port | where the camera sends the **video** (you announce it in OpenVideo) |

### Command framing (12-byte header, little-endian)

```
offset  size  field
0       4     magic    = 0xFFEEFFEE   (on the wire: EE FF EE FF)
4       2     id       increments per message; the camera echoes it back
6       2     type     message type (see below)
8       1     unk      = 1 in requests
9       1     err      = 0 means OK (in responses)
10      2     length   number of data bytes after this header
12      …     data     `length` bytes
```

**Message types**

| type | name | notes |
|------|------|-------|
| `0x01` | GetDeviceInfo | returns vendor / product / firmware / ssid |
| `0x02` | GetLicense | returns serial + on-device license (not a client gate) |
| `0x03` | SetLicense | |
| `0x04` | **OpenVideo** | start streaming (see below) |
| `0x06` | UpdateFirmware | |
| `0x0A` | SetLed | (the MS5 has a *physical* LED dial; no app control) |
| `0x0C` | CameraCommand | camera controls |
| `0x0D` | GetCameraConfig | |
| `0x0E` | SetCameraConfig | likely where resolution lives — see roadmap |

**GetDeviceInfo response data** (128 bytes): `unk0` `u8`, `vendor` `char[32]`, `product` `char[32]`,
`fw_version` `char[16]`, `ssid` `char[32]`, then power/capacity/work-mode fields.

> ⚠️ **The UDP service often drops the very first packet after idle. Always retry a few times.**

### Starting the video stream (the key handshake)

This is the one non-obvious part. OpenVideo with an empty body is acknowledged but produces **no video**,
because the camera doesn't know where to send it. You must **tell it your receive port**:

1. Bind a UDP socket to an OS-assigned **ephemeral port `P`** (read it back with `getsockname`).
2. Send **OpenVideo** (`type 0x04`) to `:10006` with `P` as a 2-byte little-endian payload:
   ```
   EE FF EE FF | id(2) | 04 00 | 01 | 00 | len(2) | P_lo P_hi
   ```
   (The reference library writes the header `length` field as `0` but still appends the 2 port bytes,
   for 14 bytes total. The camera replies `err=0`.)
3. The camera now streams the video to **`your_ip:P`**.

### Video stream format

Datagrams (~1416 bytes) arrive on `P`: a **16-byte chunk header + JPEG payload**.

```
offset  size   field
0       1      0x01     (constant for every chunk on this firmware)
1       1      n_chunk  global rolling counter (wraps at 256)
2       1      n_frame  frame id  ← a frame ends when this value changes
3       1      last_chunk flag
4       1      chunk index within the frame (1-based)
5       1      0
6       6      position (3 × u16; orientation/coords, ~0)
12      2      width   (u16 LE, e.g. 0x0500 = 1280)
14      2      height  (u16 LE, e.g. 0x02D0 = 720)
16      …      JPEG bytes
```

**Reassembly:** group datagrams by `n_frame` (byte 2), order them by chunk index (byte 4), and
concatenate `payload[16:]` to get one JPEG (`FF D8 … FF D9`). Finalize a frame when `n_frame` changes.

> Note: on this firmware byte 0 stays `0x01` for **every** chunk — do **not** use "byte 0 == 2" as the
> end-of-frame marker.

Result: **1280×720 MJPEG, ~21 fps** (~330 datagrams/s).

---

## Troubleshooting

- **No response / `connect timeout`:** make sure you're connected to the camera's WiFi AP and can reach
  `192.168.1.1`. The first request after idle is frequently dropped — the viewer retries automatically.
- **Joining the camera kills your internet:** expected. The camera AP has no uplink. The viewer is local.
- **Stream stalls after a while:** the viewer re-sends OpenVideo if it sees no data for ~1 s. If it still
  drops on very long sessions, the firmware also supports a reliable-UDP ACK layer we haven't needed to
  implement yet (see roadmap).
- **Port 45100 in use:** edit `HTTP_PORT` at the top of `ms5_viewer.py`.

---

## Roadmap / help wanted

Contributions very welcome — open an issue or PR.

- **Change resolution.** The camera exposes `GetCameraConfig`/`SetCameraConfig` (`type 0x0D`/`0x0E`) and
  the library has resolution get/set calls. The underlying sensor advertises **1920×1080, 1280×720,
  640×480, 640×400, 640×320** (from the device's USB UVC descriptors); the WiFi stream currently arrives
  at **1280×720**. Switching almost certainly works via `SetCameraConfig`, but the exact payload isn't
  decoded yet — **this is the most-wanted feature.** If you can capture the official app changing
  resolution (root `tcpdump -i wlan0`, or analysis of `SetCameraConfig`), please share it.
- **Reliable long-running streams:** implement the ACK layer (`cProACKSet` / `check_sendack` in the
  library) if needed.
- **Clean shutdown:** send a Stop/`caStop` on exit so the camera frees the session immediately.
- **Recording** (save MJPEG/AVI) and a **native desktop window** variant.
- **Other devices:** test on other i4season / "Suear" cameras and report compatibility.

---

## How it was reverse-engineered

1. The official app is broken, but the **camera is fine** — an early Android packet capture looked like a
   different protocol, but it was a non-working snapshot *and* the capture tool (a no-root VPN) is
   structurally blind to the real video, because the app binds its camera sockets to the WiFi network and
   bypasses the VPN route.
2. The app drives the camera via **`libWifiCamera.so`** (i4season). The open-source
   [Suear-Web-Viewer](https://github.com/SeanPesce/Suear-Web-Viewer) is a clean client for the *same*
   library and decoded most of the framing.
3. A live `GetDeviceInfo` to UDP `:10005` got a reply — the camera identified itself as **MS5**,
   confirming it speaks this protocol.
4. OpenVideo with no payload acked but never streamed. Disassembling `proOpenVideo` (arm64) revealed it
   **announces a client-chosen receive port inside the OpenVideo message** — the missing piece.
5. Sending OpenVideo *with the port* produced an immediate 1280×720 MJPEG stream; decoding the 16-byte
   chunk header yielded clean frames.

**Disproven path (ignore):** the app also bundles a JoyHonest "GP/GK" SDK (`libjh_wifi.so`: `JHCMD` → UDP
`:20000`, MJPEG-HTTP `:8080`). The MS5 does **not** use it.

---

## Credits

- **[Sean Pesce](https://github.com/SeanPesce)** and his
  [Suear-Web-Viewer](https://github.com/SeanPesce/Suear-Web-Viewer) — a clean-room client for the same
  `libWifiCamera.so` library that decoded most of the protocol and made this possible. (No code was
  copied; `ms5_viewer.py` is an independent implementation for the MS5's port-announce handshake and
  chunk format.)
- The elektroda.com **Taixen TXW816 otoscope teardown** community thread — independent confirmation of
  the device-side framing.

---

## License

[MIT](LICENSE). Use it, fork it, ship it.

---

## Disclaimer

This is independent **interoperability research** for hardware the authors own. It is not affiliated with
or endorsed by the device manufacturer. Names and trademarks belong to their respective owners. Use at
your own risk.
