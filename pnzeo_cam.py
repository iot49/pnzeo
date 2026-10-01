#!/usr/bin/env python3
"""Take pictures and movies from a Pnzeo W3 (ismartol / IPCamera-Web) over HTTP.

The camera at http://<ip>/ speaks a standard mjpg-streamer interface:
    /media/?action=snapshot   -> one JPEG
    /media/?action=stream     -> multipart/x-mixed-replace MJPEG
behind HTTP Basic auth. For the device web login the user is "admin" with an
empty password (the app's own password is a separate cloud-account credential).

No third-party packages: standard library only.

    python3 pnzeo_cam.py snap                 # -> snapshot_<ts>.jpg
    python3 pnzeo_cam.py snap -o front.jpg
    python3 pnzeo_cam.py record 10            # 10 s -> clip_<ts>.avi
    python3 pnzeo_cam.py record 10 -o door.avi

As a library:
    cam = Camera("192.168.178.58")
    cam.snapshot("front.jpg")
    cam.record("door.avi", seconds=10)
"""
import argparse
import base64
import os
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HOST = os.environ.get("PNZEO_HOST", "192.168.178.58")
USER = os.environ.get("PNZEO_USER", "admin")
PASSWORD = os.environ.get("PNZEO_PASS", "")  # device web password is blank


class Camera:
    def __init__(self, host=HOST, user=USER, password=PASSWORD):
        self.base = f"http://{host}"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"

    def _open(self, path, timeout):
        req = urllib.request.Request(self.base + path,
                                     headers={"Authorization": self.auth})
        return urllib.request.urlopen(req, timeout=timeout)

    def snapshot(self, path=None, timeout=10, gray=False):
        """Save a single JPEG. Returns the path written."""
        path = path or f"snapshot_{_stamp()}.jpg"
        with self._open("/media/?action=snapshot", timeout) as r:
            data = r.read()
        if gray:
            data = _to_gray(data)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def frames(self, timeout=10, gray=False):
        """Yield JPEG frames (bytes) from the MJPEG stream, forever."""
        with self._open("/media/?action=stream", timeout) as r:
            while True:
                length = _content_length(r)
                if length is None:
                    return
                jpg = _read_exact(r, length)
                yield _to_gray(jpg) if gray else jpg

    def record(self, path=None, seconds=10, timeout=10, gray=False):
        """Record the MJPEG stream to a Motion-JPEG AVI. Returns (path, nframes, fps)."""
        path = path or f"clip_{_stamp()}.avi"
        jpegs, fps = self._capture(seconds, timeout, gray)
        _write_mjpeg_avi(path, jpegs, fps)
        return path, len(jpegs), fps

    def record_mp4(self, path=None, seconds=10, timeout=10, gray=False, crf=23):
        """Record and transcode to an H.264 MP4 (much smaller). Returns (path, nframes, fps)."""
        path = path or f"clip_{_stamp()}.mp4"
        jpegs, fps = self._capture(seconds, timeout, gray)
        _encode_h264(path, jpegs, fps, crf)
        return path, len(jpegs), fps

    def _capture(self, seconds, timeout, gray):
        jpegs = []
        start = time.monotonic()
        for jpg in self.frames(timeout, gray):
            jpegs.append(jpg)
            if time.monotonic() - start >= seconds:
                break
        if not jpegs:
            raise RuntimeError("camera sent no frames; another connection may be "
                               "stalled or just closed, try again")
        elapsed = time.monotonic() - start
        fps = len(jpegs) / elapsed if elapsed > 0 else 1.0
        return jpegs, fps


# ---- Optional grayscale (the app's "B&W" toggle) ----------------------------
# In low light the camera's color frames have a magenta cast: IR contaminates
# the color channels and that is not invertible. Desaturating to gray discards
# the broken color, matching what the app's B&W button does. Needs Pillow:
#   uv run --with pillow pnzeo_cam.py snap --gray

def _to_gray(jpeg_bytes):
    import io
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("--gray needs Pillow: run with  uv run --with pillow ...")
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue()


# ---- MJPEG multipart parsing ------------------------------------------------

def _content_length(fp):
    """Read multipart part headers, return the body length, or None at stream end."""
    length = None
    headers = False
    while True:
        line = fp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if headers:  # blank line after headers ends them
                return length
            continue  # CRLF before the boundary
        headers = True
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])


def _read_exact(fp, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = fp.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# ---- Minimal Motion-JPEG AVI writer (stdlib only) ---------------------------
# Plays in QuickTime, VLC, and ffmpeg. Frames keep the camera's own resolution.

def _jpeg_size(data):
    """Return (width, height) from a JPEG's SOF marker."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = struct.unpack(">H", data[i + 5:i + 7])[0]
            w = struct.unpack(">H", data[i + 7:i + 9])[0]
            return w, h
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg
    return 0, 0


def _write_mjpeg_avi(path, frames, fps):
    if not frames:
        raise RuntimeError("no frames captured")
    width, height = _jpeg_size(frames[0])
    rate = max(1, round(fps * 1000))  # strh rate/scale = fps, scale 1000
    us_per_frame = int(1_000_000 / fps) if fps > 0 else 1_000_000

    def chunk(fourcc, payload):
        pad = b"\x00" if len(payload) & 1 else b""
        return fourcc + struct.pack("<I", len(payload)) + payload + pad

    # movi: each frame as a '00dc' chunk
    parts = [b"movi"]
    index = []
    offset = 4  # relative to movi_body start, after the 'movi' fourcc
    for jpg in frames:
        index.append((offset, len(jpg)))
        parts.append(chunk(b"00dc", jpg))
        offset += 8 + len(jpg) + (len(jpg) & 1)
    movi_body = b"".join(parts)
    movi = b"LIST" + struct.pack("<I", len(movi_body)) + movi_body

    idx1 = b"".join(struct.pack("<4sIII", b"00dc", 0x10, off, size)
                    for off, size in index)
    idx1 = chunk(b"idx1", idx1)

    max_bytes = max(len(f) for f in frames)
    avih = struct.pack("<IIIIIIIIIIIIII",
                       us_per_frame, 0, 0, 0x10, len(frames), 0, 1,
                       max_bytes, width, height, 0, 0, 0, 0)
    strh = struct.pack("<4s4sIHHIIIIIIIIhhhh",
                       b"vids", b"MJPG", 0, 0, 0, 0, 1000, rate, 0, len(frames),
                       max_bytes, 0xFFFFFFFF, 0, 0, 0, width, height)
    strf = struct.pack("<IiiHH4sIiiII",
                       40, width, height, 1, 24, b"MJPG",
                       width * height * 3, 0, 0, 0, 0)
    strl = b"LIST" + struct.pack("<I", len(b"strl" + chunk(b"strh", strh)
                                 + chunk(b"strf", strf))) \
           + b"strl" + chunk(b"strh", strh) + chunk(b"strf", strf)
    hdrl_body = b"hdrl" + chunk(b"avih", avih) + strl
    hdrl = b"LIST" + struct.pack("<I", len(hdrl_body)) + hdrl_body

    body = b"AVI " + hdrl + movi + idx1
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


# ---- Optional H.264 transcode (smaller files than Motion-JPEG) --------------
# Pipe the captured JPEGs into ffmpeg as an MJPEG stream and re-encode to H.264.
# This is lossy -> lossy (a second compression generation), so a touch worse
# than the camera's own H.264 would be, but far smaller than the AVI. Needs an
# ffmpeg binary; imageio-ffmpeg bundles one:
#   uv run --with imageio-ffmpeg pnzeo_cam.py record 10 --mp4

def _ffmpeg_exe():
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise SystemExit("--mp4 needs ffmpeg: run with  "
                         "uv run --with imageio-ffmpeg ...  or install ffmpeg")


def _encode_h264(path, frames, fps, crf):
    if not frames:
        raise RuntimeError("no frames captured")
    cmd = [_ffmpeg_exe(), "-y", "-f", "mjpeg", "-framerate", f"{fps:.3f}",
           "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
           "-pix_fmt", "yuv420p", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, err = p.communicate(b"".join(frames))
    if p.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + err.decode("utf-8", "replace")[-800:])


def _stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def main():
    ap = argparse.ArgumentParser(description="Pnzeo W3 camera: pictures and movies over HTTP")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--password", default=PASSWORD)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snap", help="save a single JPEG")
    s.add_argument("-o", "--out")
    s.add_argument("--gray", action="store_true", help="desaturate (needs Pillow)")

    r = sub.add_parser("record", help="record the stream to an AVI (or MP4 with --mp4)")
    r.add_argument("seconds", type=float)
    r.add_argument("-o", "--out")
    r.add_argument("--gray", action="store_true", help="desaturate (needs Pillow)")
    r.add_argument("--mp4", action="store_true",
                   help="transcode to H.264 MP4, smaller files (needs ffmpeg)")
    r.add_argument("--crf", type=int, default=23,
                   help="H.264 quality for --mp4, lower is better (default 23)")

    a = ap.parse_args()
    cam = Camera(a.host, a.user, a.password)
    if a.cmd == "snap":
        path = cam.snapshot(a.out, gray=a.gray)
        print(f"wrote {path}")
    elif a.cmd == "record":
        if a.mp4:
            path, n, fps = cam.record_mp4(a.out, a.seconds, gray=a.gray, crf=a.crf)
        else:
            path, n, fps = cam.record(a.out, a.seconds, gray=a.gray)
        print(f"wrote {path}  ({n} frames, {fps:.1f} fps)")


if __name__ == "__main__":
    main()
