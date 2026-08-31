"""Cover-art colour extraction.

The naive average of an album cover is almost always a muddy grey-brown
— opposing hues cancel out. `dominant_color` buckets by hue and weights
by saturation instead, so the colour it returns is the one a person
would point at. These tests pin that behaviour, because the failure mode
is subtle: the app still works, it just looks like mud.
"""
import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf  # noqa: E402

from jamjar.palette import dominant_color, tint_color  # noqa: E402


def _png(fill: int, patch: int | None = None, patch_size: int = 24) -> bytes:
    """A 64×64 PNG of `fill`, optionally with a centred `patch` block.

    Colours are 0xRRGGBBAA, the order GdkPixbuf.fill expects.
    """
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64)
    pixbuf.fill(fill)
    if patch is not None:
        block = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                                     patch_size, patch_size)
        block.fill(patch)
        block.copy_area(0, 0, patch_size, patch_size, pixbuf, 20, 20)
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    assert ok
    return data


def _hue_family(rgb):
    r, g, b = rgb
    return max(("r", r), ("g", g), ("b", b), key=lambda pair: pair[1])[0]


# --- dominant_color ----------------------------------------------------

def test_finds_the_ink_not_the_sleeve():
    # A mostly-white cover with a red block: the red is the album's colour.
    assert _hue_family(dominant_color(_png(0xFFFFFFFF, 0xD02020FF))) == "r"


def test_finds_colour_on_a_dark_cover():
    assert _hue_family(dominant_color(_png(0x080808FF, 0x2050E0FF))) == "b"


def test_a_saturated_minority_beats_a_washed_out_majority():
    # Pale blue everywhere, a small vivid green patch. Weighting by
    # saturation is what makes the green win.
    assert _hue_family(dominant_color(_png(0xCCD8E8FF, 0x18C020FF))) == "g"


def test_greyscale_cover_returns_a_grey():
    r, g, b = dominant_color(_png(0x808080FF))
    assert abs(r - g) <= 2 and abs(g - b) <= 2


def test_undecodable_data_returns_none():
    assert dominant_color(b"not an image") is None


# --- tint_color --------------------------------------------------------

def test_dark_tint_is_darker_than_light_tint():
    rgb = (208, 32, 32)
    assert sum(tint_color(rgb, dark=True)) < sum(tint_color(rgb, dark=False))


def test_tint_keeps_the_hue():
    assert _hue_family(tint_color((208, 32, 32), dark=True)) == "r"
    assert _hue_family(tint_color((32, 80, 208), dark=False)) == "b"


def test_tint_tames_a_neon_cover():
    # Pure magenta must not come back as pure magenta — it sits behind
    # text.
    r, g, b = tint_color((255, 0, 255), dark=True)
    assert min(r, g, b) > 0
