import io
import struct

import pytest

import pnzeo_cam as p

JPG = b"\xff\xd8" + b"x" * 101 + b"\xff\xd9"
HEAD = b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(JPG)


def _parse(stream):
    fp = io.BufferedReader(io.BytesIO(stream))
    frames = []
    while (n := p._content_length(fp)) is not None:
        frames.append(p._read_exact(fp, n))
    return frames


@pytest.mark.parametrize(
    "part",
    [
        b"--b\r\n" + HEAD + JPG,  # no CRLF around the boundary
        b"--b\r\n" + HEAD + JPG + b"\r\n",  # CRLF after the body
        b"\r\n--b\r\n" + HEAD + JPG,  # CRLF before the boundary
    ],
)
def test_parse_stream_framings(part):
    assert _parse(part * 3) == [JPG] * 3


def test_parse_part_without_content_length_ends_stream():
    assert _parse(b"--b\r\nContent-Type: image/jpeg\r\n\r\n" + JPG) == []


def test_avi_header_keeps_fractional_fps(tmp_path):
    path = tmp_path / "a.avi"
    p._write_mjpeg_avi(path, [JPG] * 5, 9.4)
    data = path.read_bytes()
    i = data.index(b"strh") + 8
    scale, rate = struct.unpack("<II", data[i + 20 : i + 28])
    assert rate / scale == pytest.approx(9.4)


def test_avi_index_points_at_frames(tmp_path):
    path = tmp_path / "a.avi"
    frames = [JPG, JPG + b"\0", JPG]  # odd and even lengths
    p._write_mjpeg_avi(path, frames, 9.0)
    data = path.read_bytes()
    movi = data.index(b"movi")
    idx = data.index(b"idx1") + 8
    for k, jpg in enumerate(frames):
        _, _, off, size = struct.unpack(
            "<4sIII", data[idx + 16 * k : idx + 16 * (k + 1)]
        )
        assert data[movi + off : movi + off + 4] == b"00dc"
        assert data[movi + off + 8 : movi + off + 8 + size] == jpg


def test_ffmpeg_failure_reports_its_error(tmp_path, monkeypatch):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(p, "_ffmpeg_exe", lambda: str(fake))
    frames = [b"\0" * 1_000_000] * 4  # more than a pipe buffer
    with pytest.raises(SystemExit, match="boom"):
        p._encode_h264(tmp_path / "out.mp4", frames, 9.0, 23)
