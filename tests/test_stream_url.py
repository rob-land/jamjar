"""Stream URL construction — including the server-side seek offset.

Testing against a real server turned up two things a mock never would:
this Jellyfin answers with `Accept-Ranges: none` and no Content-Length,
so GStreamer can neither report a duration nor seek. `startTimeTicks` is
the only seek available, which makes the tick maths here load-bearing:
get the conversion wrong and the scrubber silently jumps to the wrong
place.
"""
import gi

gi.require_version("Gst", "1.0")

from urllib.parse import parse_qs, urlparse  # noqa: E402

from jamjar.client import TICKS_PER_SECOND, JellyfinClient  # noqa: E402
from jamjar.models import Track  # noqa: E402


def _client():
    return JellyfinClient("https://example.invalid/", "user-1", "tok", "dev-1")


def _track():
    return Track(id="item-1", name="Song", album="", album_id="", artists=(),
                 artist_ids=(), duration_ticks=0)


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_url_targets_the_universal_audio_endpoint():
    assert urlparse(_client().stream_url(_track())).path == "/Audio/item-1/universal"


def test_default_url_has_no_start_offset():
    assert "startTimeTicks" not in _params(_client().stream_url(_track()))


def test_start_seconds_becomes_ticks():
    url = _client().stream_url(_track(), start_seconds=90)
    assert _params(url)["startTimeTicks"] == str(90 * TICKS_PER_SECOND)


def test_fractional_seconds_truncate_to_whole_ticks():
    url = _client().stream_url(_track(), start_seconds=1.5)
    assert _params(url)["startTimeTicks"] == str(15_000_000)


def test_zero_start_is_omitted_rather_than_sent_as_zero():
    # A pointless startTimeTicks=0 defeats the response cache for no gain.
    assert "startTimeTicks" not in _params(_client().stream_url(_track(), start_seconds=0))


def test_static_downloads_ask_for_the_original_file():
    assert _params(_client().stream_url(_track(), static=True))["static"] == "true"


def test_bitrate_is_only_sent_when_capped():
    assert "maxStreamingBitrate" not in _params(_client().stream_url(_track()))
    url = _client().stream_url(_track(), max_bitrate=128000)
    assert _params(url)["maxStreamingBitrate"] == "128000"


def test_credentials_and_device_ride_along():
    params = _params(_client().stream_url(_track()))
    assert params["api_key"] == "tok"
    assert params["userId"] == "user-1"
    assert params["deviceId"] == "dev-1"
