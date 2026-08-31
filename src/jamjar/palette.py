"""Dominant-colour extraction from cover art.

Amberol's trick: tint the player with the artwork so the app takes on the
character of what's playing. The colour has to be picked carefully — the
naive average of an album cover is almost always a muddy grey-brown,
because averaging opposing hues cancels them out.

So instead of averaging everything, pixels are bucketed by hue and each
bucket weighted by saturation: the winning bucket is the colour a person
would point at, not the arithmetic middle. Near-black and near-white
pixels are skipped entirely — they're background, and a cover that is
80% white sleeve should still be tinted by the ink on it.
"""

from __future__ import annotations

import colorsys
import logging

from gi.repository import GdkPixbuf, GLib

log = logging.getLogger(__name__)

# The sample is tiny on purpose: 32×32 is plenty to find a dominant hue
# and keeps the whole pass well under a millisecond.
SAMPLE_SIZE = 32
HUE_BUCKETS = 24

# Pixels outside these bounds carry no usable hue.
MIN_VALUE = 0.12
MAX_VALUE = 0.97
MIN_SATURATION = 0.12

# The tint sits behind text, so the colour is pinned to a readable band
# rather than used raw — a neon cover shouldn't produce a neon page.
DARK_VALUE = 0.42
LIGHT_VALUE = 0.86
MAX_TINT_SATURATION = 0.62


def _pixbuf_from_bytes(data: bytes) -> GdkPixbuf.Pixbuf | None:
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.set_size(SAMPLE_SIZE, SAMPLE_SIZE)
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()
    except GLib.Error as e:
        log.debug("palette: image decode failed: %s", e)
        return None


def dominant_color(data: bytes) -> tuple[int, int, int] | None:
    """The most prominent saturated colour in `data`, as 0–255 RGB."""
    pixbuf = _pixbuf_from_bytes(data)
    if pixbuf is None:
        return None

    pixels = pixbuf.get_pixels()
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    width, height = pixbuf.get_width(), pixbuf.get_height()

    # bucket -> [weight, r_sum, g_sum, b_sum]
    buckets: dict[int, list[float]] = {}
    fallback = [0.0, 0.0, 0.0, 0.0]

    for y in range(height):
        row = y * stride
        for x in range(width):
            offset = row + x * channels
            r, g, b = pixels[offset], pixels[offset + 1], pixels[offset + 2]
            if channels == 4 and pixels[offset + 3] < 128:
                continue
            fallback[0] += 1
            fallback[1] += r
            fallback[2] += g
            fallback[3] += b
            hue, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            if light < MIN_VALUE or light > MAX_VALUE or sat < MIN_SATURATION:
                continue
            bucket = int(hue * HUE_BUCKETS) % HUE_BUCKETS
            # Squared saturation, discounted away from mid-lightness. A
            # large pale wash is usually sleeve or paper; the colour worth
            # tinting with is the vivid mid-tone, even when it covers less
            # of the cover. Area still counts — it's one vote per pixel.
            weight = sat * sat * max(0.1, 1.0 - abs(light - 0.5) * 1.6)
            entry = buckets.setdefault(bucket, [0.0, 0.0, 0.0, 0.0])
            entry[0] += weight
            entry[1] += r * weight
            entry[2] += g * weight
            entry[3] += b * weight

    if buckets:
        weight, r_sum, g_sum, b_sum = max(buckets.values(), key=lambda e: e[0])
    elif fallback[0]:
        # A greyscale cover: no hue to find, so the plain average is the
        # honest answer.
        weight, r_sum, g_sum, b_sum = fallback
    else:
        return None

    return (int(r_sum / weight), int(g_sum / weight), int(b_sum / weight))


def tint_color(rgb: tuple[int, int, int], *, dark: bool) -> tuple[int, int, int]:
    """Push `rgb` into the lightness band that stays readable behind text."""
    r, g, b = (c / 255 for c in rgb)
    hue, _light, sat = colorsys.rgb_to_hls(r, g, b)
    sat = min(sat, MAX_TINT_SATURATION)
    light = DARK_VALUE if dark else LIGHT_VALUE
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return (int(r * 255), int(g * 255), int(b * 255))
