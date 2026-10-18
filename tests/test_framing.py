import pytest

from acs.core import framing
from acs.core.model import CropRect

DAR = 16 / 9


def test_parse_aspect():
    assert framing.parse_aspect("16:9", DAR) == pytest.approx(16 / 9)
    assert framing.parse_aspect("source", 1.5) == 1.5
    assert framing.parse_aspect("free", DAR) is None and framing.parse_aspect("bad", DAR) is None


def test_clamp_keeps_a_locked_ratio_inside_the_picture():
    x, y, w, h = framing.clamp_rect((0.9, -0.2, 0.5, 0.7), DAR, 1.0)
    assert w * DAR / h == pytest.approx(1.0) and h == pytest.approx(0.7)
    assert x + w == pytest.approx(1.0) and y == 0.0
    # a free rectangle only gets limited and moved
    assert framing.clamp_rect((0.5, 0.5, 2.0, 0.01), DAR) == (0.0, 0.5, 1.0, 0.05)


def test_largest_ratio_inside_a_rectangle():
    r = framing.fit_aspect_inside((0.0, 0.0, 1.0, 1.0), 1.0, DAR)    # square in a 16:9 picture
    assert r == pytest.approx((0.21875, 0.0, 0.5625, 1.0))
    assert framing.centered_max(1.0, DAR) == pytest.approx(r)
    assert framing.centered_max(None, DAR) == (0.0, 0.0, 1.0, 1.0)


def test_pixels_are_even_and_keep_the_ratio():
    c = framing.to_crop(framing.centered_max(1.0, DAR), 1920, 1080, "1:1", 1.0)
    assert c == CropRect(420, 0, 1080, 1080)
    c = framing.to_crop((0.1, 0.1, 0.333, 0.5), 1920, 1080)
    assert all(v % 2 == 0 for v in (c.x, c.y, c.w, c.h))
    assert framing.from_crop(CropRect(192, 108, 960, 540), 1920, 1080) == (0.1, 0.1, 0.5, 0.5)
