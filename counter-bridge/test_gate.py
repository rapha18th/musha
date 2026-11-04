"""The bridge's gate and link, as pure functions: python -m pytest counter-bridge/test_gate.py

Runs with the backend's virtual environment; OpenCV and requests load only when the bridge runs."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import counter_bridge  # noqa: E402
from counter_bridge import Gate, Look, State, decide, measure, moved, parse_link, sent  # noqa: E402

W, H = 32, 24


def flat(level=120):
    return [level] * (W * H)


def with_box(level=120, box=250, left=8, top=6, right=20, bottom=18):
    return [box if left <= x < right and top <= y < bottom else level for y in range(H) for x in range(W)]


def look(thumb, brightness=120.0, sharpness=10.0):
    return Look(brightness, sharpness, thumb)


def test_the_gate_loads_on_its_own():
    # OpenCV and requests load inside Camera and Musha, when the bridge runs
    assert not hasattr(counter_bridge, "cv2") and not hasattr(counter_bridge, "requests")


def test_moved_takes_the_exposure_shift_out():
    a = with_box()
    assert moved(a, a) == 0
    assert moved(a, [v + 30 for v in a]) == 0  # the camera brightened: nothing moved
    assert moved(flat(), with_box()) == pytest.approx(12 * 12 * 130 / (W * H))


def test_measure_reads_light_and_focus():
    assert measure(flat(0), W, H) == (0.0, 0.0)
    b, s = measure(flat(200), W, H)
    assert b == 200 and s == 0
    checker = [255 if (x + y) % 2 else 0 for y in range(H) for x in range(W)]
    assert measure(checker, W, H)[1] == 255
    assert measure([], 0, 0) == (0.0, 0.0)


def test_the_first_picture_goes_at_once():
    assert decide(State(), look(flat()), 0.0, Gate()) == (True, "first")


def test_a_change_sends_and_the_same_picture_waits():
    g, s = Gate(), State()
    sent(s, look(flat()), 0.0)
    assert decide(s, look(flat()), 20.0, g) == (False, "same")
    assert decide(s, look(with_box()), 20.0, g) == (True, "changed")


def test_changes_send_at_most_every_eight_seconds():
    g, s = Gate(), State()
    sent(s, look(flat()), 0.0)
    assert decide(s, look(with_box()), 5.0, g) == (False, "wait")
    assert decide(s, look(with_box()), 8.0, g) == (True, "changed")


def test_a_still_goes_every_three_minutes_whatever_the_picture():
    g, s = Gate(), State()
    sent(s, look(flat()), 0.0)
    assert decide(s, look(flat(), brightness=5, sharpness=0), 179.0, g)[0] is False
    assert decide(s, look(flat(), brightness=5, sharpness=0), 180.0, g) == (True, "heartbeat")


def test_dark_and_blurred_pictures_wait_for_the_heartbeat():
    g, s = Gate(), State()
    sent(s, look(flat(), sharpness=10.0), 0.0)
    assert decide(s, look(with_box(level=10, box=20), brightness=12), 30.0, g) == (False, "dark")
    assert decide(s, look(with_box(), sharpness=0.5), 30.0, g) == (False, "blurred")  # a stream lost its picture
    assert decide(s, look(with_box(), sharpness=4.0), 30.0, g) == (False, "blurred")  # far softer than the last
    assert decide(s, look(with_box(), sharpness=6.0), 30.0, g) == (True, "changed")


def test_sending_moves_the_base():
    s = State()
    sent(s, look(with_box(), sharpness=7.5), 42.0)
    assert (s.base, s.base_sharpness, s.sent_at, s.sent) == (with_box(), 7.5, 42.0, 1)


def test_the_counter_link_gives_the_site_the_shop_and_the_key():
    assert parse_link("https://musha.example/#/counter?shop=12&key=abc-DEF_123") == \
        ("https://musha.example", 12, "abc-DEF_123")
    assert parse_link("http://localhost:5271/#/counter?shop=3&key=k%2Fey") == ("http://localhost:5271", 3, "k/ey")
    assert parse_link(" http://host:8000/app/#counter?key=x&shop=7 ") == ("http://host:8000/app", 7, "x")


@pytest.mark.parametrize("link", [
    "musha.example/#/counter?shop=1&key=k",  # no scheme
    "https://musha.example/#/shop?shop=1&key=k",  # the owner's console
    "https://musha.example/#/counter?shop=one&key=k",
    "https://musha.example/#/counter?shop=1",
    "rtsp://camera/#/counter?shop=1&key=k",
])
def test_other_links_are_refused(link):
    with pytest.raises(ValueError):
        parse_link(link)
