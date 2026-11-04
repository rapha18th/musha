"""Musha counter bridge: an IP camera, a webcam or a video file as the shop's counter camera.

It runs on a computer beside the camera; a Raspberry Pi is enough. It reads the stream with
OpenCV and looks once a second at a small grey copy of the picture. A still goes to Musha when
the picture changed, and one goes at least every three minutes. A dark or blurred picture waits
for the three-minute still. Stills go to the same route the counter phone uses, with the shop's
counter key, and the bridge answers the owner's Look in within a few seconds.

    python counter_bridge.py --link "<counter link>" --camera "rtsp://user:pass@192.168.1.64:554/Streaming/Channels/102"

The counter link comes from Connect your shop in the owner's console. Musha keeps a still only
after the manager agrees to the camera; the first run prints the terms, and --agree with
--name records the manager's agreement. Every option can also come from the environment
(MUSHA_LINK, MUSHA_CAMERA, MUSHA_NAME), which suits a service.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

THUMB = (32, 24)  # the picture compared with the last one sent, as on the counter phone
LOOK = (160, 120)  # the picture measured for light and focus
STILL_WIDTH = 640  # the server keeps stills at 640 px on the longer side
QUALITY = 80


# The gate: pure functions. They run and test on any computer, camera or none.

@dataclass
class Look:
    brightness: float  # mean grey level, 0 to 255
    sharpness: float  # mean step between neighbouring grey pixels
    thumb: list[int]  # 32 x 24 grey levels


@dataclass
class Gate:
    change: float = 6.0  # grey levels the picture must move on average, after the exposure shift
    min_gap: float = 8.0  # seconds between stills sent on a change
    every: float = 180.0  # seconds: a still goes at least this often, whatever the picture
    dark: float = 25.0  # a picture darker than this waits for the heartbeat still
    blur_floor: float = 1.0  # a flat picture (a stream that lost its picture) waits too
    blur_ratio: float = 0.5  # as does one this much blurrier than the last still sent


@dataclass
class State:
    base: list[int] | None = None
    base_sharpness: float = 0.0
    sent_at: float | None = None
    sent: int = 0
    skipped: dict = field(default_factory=dict)


def measure(grey: list[int], width: int, height: int) -> tuple[float, float]:
    """Brightness and sharpness of a grey picture given row by row."""
    if not grey:
        return 0.0, 0.0
    brightness = sum(grey) / len(grey)
    steps, n = 0, 0
    for y in range(height):
        row = grey[y * width:(y + 1) * width]
        for a, b in zip(row, row[1:]):
            steps += abs(b - a)
            n += 1
    for y in range(height - 1):
        for x in range(width):
            steps += abs(grey[(y + 1) * width + x] - grey[y * width + x])
            n += 1
    return brightness, (steps / n if n else 0.0)


def moved(before: list[int], after: list[int]) -> float:
    """Mean absolute difference, with the median brightness shift (the camera's exposure) taken out."""
    diffs = [b - a for a, b in zip(before, after)]
    if not diffs:
        return 0.0
    shift = sorted(diffs)[(len(diffs) - 1) // 2]
    return sum(abs(d - shift) for d in diffs) / len(diffs)


def decide(state: State, look: Look, at: float, gate: Gate) -> tuple[bool, str]:
    """Should this picture go to Musha now? Returns the answer and the reason."""
    since = float("inf") if state.sent_at is None else at - state.sent_at
    if since >= gate.every:
        return True, "heartbeat" if state.sent_at is not None else "first"
    if look.brightness < gate.dark:
        return False, "dark"
    if look.sharpness < max(gate.blur_floor, gate.blur_ratio * state.base_sharpness):
        return False, "blurred"
    if since < gate.min_gap:
        return False, "wait"
    if state.base is None or moved(state.base, look.thumb) >= gate.change:
        return True, "changed"
    return False, "same"


def sent(state: State, look: Look, at: float) -> None:
    state.base, state.base_sharpness, state.sent_at = look.thumb, look.sharpness, at
    state.sent += 1


def parse_link(link: str) -> tuple[str, int, str]:
    """The counter link, https://<site>/#/counter?shop=<id>&key=<key>, as (site, shop id, key)."""
    u = urlparse(link.strip())
    query = parse_qs(u.fragment.split("?", 1)[1] if "?" in u.fragment else "")
    shop, key = (query.get("shop") or [""])[0], (query.get("key") or [""])[0]
    if u.scheme not in ("http", "https") or not u.netloc or not u.fragment.lstrip("/").startswith("counter"):
        raise ValueError("The counter link looks like https://<site>/#/counter?shop=<id>&key=<key>.")
    if not shop.isdigit() or not key:
        raise ValueError("The counter link carries the shop and its counter key. Copy it again from Connect your shop.")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}", int(shop), key


# The camera

class Camera:
    """An RTSP or HTTP stream, a webcam number or a video file, reopened with backoff when it drops.
    A video file plays at its own speed and loops, as a camera would."""

    def __init__(self, source: str):
        import cv2

        self.cv2 = cv2
        self.source: str | int = int(source) if source.isdigit() else source
        self.file = isinstance(self.source, str) and os.path.isfile(self.source)
        self.cap = None
        self.backoff = 2.0
        self.started = time.monotonic()

    def _open(self) -> bool:
        cv2 = self.cv2
        if isinstance(self.source, str) and self.source.startswith("rtsp"):
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        self.cap = cv2.VideoCapture(self.source)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.backoff = 2.0
            print("camera open")
            return True
        print(f"camera unreachable, trying again in {self.backoff:.0f} s")
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, 60)
        return False

    def read(self):
        cv2 = self.cv2
        if self.cap is None or not self.cap.isOpened():
            if not self._open():
                return None
        if self.file:
            frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 25
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, int((time.monotonic() - self.started) * fps) % frames)
            ok, frame = self.cap.read()
            return frame if ok else None
        for _ in range(4):  # a live stream buffers; drop the backlog so the picture is current
            self.cap.grab()
        ok, frame = self.cap.retrieve()
        if not ok:
            print("stream dropped, reconnecting")
            self.cap.release()
            self.cap = None
            return None
        return frame

    def look(self, frame) -> Look:
        cv2 = self.cv2
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(grey, LOOK, interpolation=cv2.INTER_AREA)
        thumb = cv2.resize(grey, THUMB, interpolation=cv2.INTER_AREA)
        brightness, sharpness = measure(small.ravel().tolist(), *LOOK)
        return Look(brightness, sharpness, thumb.ravel().tolist())

    def jpeg(self, frame) -> bytes:
        cv2 = self.cv2
        h, w = frame.shape[:2]
        if w > STILL_WIDTH:
            frame = cv2.resize(frame, (STILL_WIDTH, round(h * STILL_WIDTH / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
        if not ok:
            raise RuntimeError("the still failed to encode")
        return buf.tobytes()

    def close(self):
        if self.cap is not None:
            self.cap.release()


# Musha

class Closed(Exception):
    """The shop refused this bridge: the link was replaced, or the manager has yet to agree."""


class Musha:
    def __init__(self, site: str, shop: int, key: str):
        import requests

        self.requests = requests
        self.base = f"{site}/api/shops/{shop}"
        self.http = requests.Session()
        self.http.headers["X-Shop-Key"] = key

    def _answer(self, r):
        if r.status_code == 403:
            raise Closed("The shop has a new counter link. Copy it from Connect your shop and start again.")
        if r.status_code == 409:
            raise Closed(_detail(r) or "The manager has yet to agree to the counter camera.")
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After") or 10)
            print(f"Musha is busy, waiting {wait} s")
            time.sleep(wait)
            return None
        if r.status_code >= 400:
            raise RuntimeError(_detail(r) or f"Musha answered {r.status_code}")
        return r.json()

    def status(self) -> dict:
        return self._answer(self.http.get(f"{self.base}/camera", timeout=30)) or {}

    def agree(self, name: str) -> dict:
        return self._answer(self.http.post(f"{self.base}/camera/consent", json={"by": name}, timeout=30)) or {}

    def beat(self) -> dict:
        return self._answer(self.http.post(f"{self.base}/camera/heartbeat", json={"source": "bridge"}, timeout=30)) or {}

    def still(self, image: bytes, kind: str) -> dict | None:
        files = {"image": ("still.jpg", image, "image/jpeg")}
        return self._answer(self.http.post(f"{self.base}/camera/frames", files=files,
                                           data={"kind": kind, "source": "bridge"}, timeout=90))


def _detail(r) -> str:
    try:
        d = r.json().get("detail")
        return d if isinstance(d, str) else ""
    except ValueError:
        return ""


def consent(musha: Musha, a) -> bool:
    """Show the terms; record the manager's agreement when asked. True once the camera may send."""
    s = musha.status()
    print("Terms the manager agrees to:")
    print(f"  {s.get('consent', '')}")
    if s.get("consented"):
        print(s.get("consent_line") or "The manager has agreed.")
        return True
    if not a.agree:
        print('Musha keeps stills once the manager agrees. With the manager\'s yes, run again with --agree --name "<name>".')
        return False
    s = musha.agree(a.name)
    print(s.get("consent_line") or f"{a.name} agreed.")
    return bool(s.get("consented"))


def send(cam: Camera, musha: Musha, frame, kind: str, why: str) -> None:
    out = musha.still(cam.jpeg(frame), kind)
    if out is None:
        return
    line = out.get("line") or ("the same picture as the last one" if out.get("kept") is False else "read later")
    print(f"{time.strftime('%H:%M:%S')} still sent ({why}): {line}")


def run(a) -> int:
    try:
        site, shop, key = parse_link(a.link)
    except ValueError as e:
        print(e)
        return 2
    musha = Musha(site, shop, key)
    print(f"Musha counter bridge for shop {shop} at {site}")
    try:
        if not consent(musha, a):
            return 3
    except (Closed, RuntimeError) as e:
        print(e)
        return 2
    except musha.requests.RequestException as e:
        print(f"Musha is out of reach: {e}")
        return 1

    cam = Camera(a.camera)
    gate = Gate(change=a.change, min_gap=a.min_gap, every=a.every)
    state = State()
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    beat_at = 0.0
    try:
        while not stop["now"]:
            started = time.monotonic()
            frame = cam.read()
            if frame is None:
                continue
            look = cam.look(frame)
            try:
                if started - beat_at >= a.beat:
                    beat_at = started
                    if musha.beat().get("look_in_requested"):
                        send(cam, musha, frame, "look_in", "look in")
                        sent(state, look, time.monotonic())  # the gap counts from the answer: a read takes seconds
                        continue
                go, why = decide(state, look, started, gate)
                if go:
                    send(cam, musha, frame, "sample", why)
                    sent(state, look, time.monotonic())
                    if a.once:
                        return 0
                else:
                    state.skipped[why] = state.skipped.get(why, 0) + 1
            except Closed as e:
                print(e)
                return 2
            except (musha.requests.RequestException, RuntimeError) as e:
                print(f"Musha is out of reach, still watching: {e}")
            time.sleep(max(0.0, a.look - (time.monotonic() - started)))
    finally:
        cam.close()
        print(f"stopped after {state.sent} stills; held back: {state.skipped or 'none'}")
    return 0


def settings(argv=None):
    env = os.environ.get
    p = argparse.ArgumentParser(description="Use an IP camera, a webcam or a video file as Musha's counter camera.")
    p.add_argument("--link", default=env("MUSHA_LINK"), help="the counter link from Connect your shop")
    p.add_argument("--camera", default=env("MUSHA_CAMERA"),
                   help="an RTSP or HTTP stream address, a webcam number such as 0, or a video file")
    p.add_argument("--agree", action="store_true", help="record the manager's agreement to the counter camera")
    p.add_argument("--name", default=env("MUSHA_NAME", ""), help="the name of the manager who agreed")
    p.add_argument("--once", action="store_true", help="send one still and stop, to test a setup")
    p.add_argument("--look", type=float, default=1.0, help="seconds between looks at the picture")
    p.add_argument("--beat", type=float, default=5.0, help="seconds between heartbeats, which carry Look in")
    p.add_argument("--min-gap", type=float, default=8.0, help="seconds between stills sent on a change")
    p.add_argument("--every", type=float, default=180.0, help="seconds: a still goes at least this often")
    p.add_argument("--change", type=float, default=6.0, help="grey levels of change that send a still")
    a = p.parse_args(argv)
    if not a.link:
        p.error("give --link, the counter link from Connect your shop")
    if not a.camera:
        p.error("give --camera")
    if a.agree and not a.name.strip():
        p.error('--agree records the manager\'s agreement: add --name "<manager\'s name>"')
    return a


if __name__ == "__main__":
    sys.exit(run(settings()))
