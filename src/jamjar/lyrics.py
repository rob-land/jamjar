"""Fetch and parse Jellyfin lyrics responses (timestamped LRC or plain)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

from .client import JellyfinClient
from .models import Track

log = logging.getLogger(__name__)

LRC_LINE_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


@dataclass(frozen=True)
class LyricLine:
    seconds: float | None   # None = unsynced
    text: str


@dataclass(frozen=True)
class Lyrics:
    lines: tuple[LyricLine, ...]
    synced: bool


def parse_lrc(text: str) -> Lyrics:
    lines: list[LyricLine] = []
    synced = False
    for raw in text.splitlines():
        match = LRC_LINE_RE.match(raw.strip())
        if match:
            synced = True
            mins, secs, body = match.group(1), match.group(2), match.group(3)
            seconds = int(mins) * 60 + float(secs)
            lines.append(LyricLine(seconds=seconds, text=body.strip()))
        elif raw.strip():
            lines.append(LyricLine(seconds=None, text=raw.strip()))
    return Lyrics(lines=tuple(lines), synced=synced)


def parse_jellyfin_lyrics(payload) -> Lyrics:
    """Jellyfin returns either {Lyrics: [{Start, Text}, ...]} or raw text."""
    if isinstance(payload, dict) and "Lyrics" in payload:
        synced = False
        lines = []
        for item in payload["Lyrics"]:
            text = (item.get("Text") or "").strip()
            start = item.get("Start")
            if start is not None:
                synced = True
                lines.append(LyricLine(seconds=start / 10_000_000, text=text))
            else:
                lines.append(LyricLine(seconds=None, text=text))
        return Lyrics(lines=tuple(lines), synced=synced)
    if isinstance(payload, str):
        return parse_lrc(payload)
    return Lyrics(lines=(), synced=False)


async def fetch_lyrics(client: JellyfinClient, track: Track) -> Lyrics | None:
    url = client.lyrics_url(track)
    try:
        async with client.session.get(url, headers=client.headers) as r:
            if r.status == 404:
                return None
            r.raise_for_status()
            try:
                payload = await r.json()
            except (aiohttp.ContentTypeError, ValueError):
                payload = await r.text()
    except aiohttp.ClientError as e:
        log.warning("lyrics fetch failed: %s", e)
        return None

    return parse_jellyfin_lyrics(payload)


def active_index(lyrics: Lyrics, position_seconds: float) -> int | None:
    if not lyrics.synced or not lyrics.lines:
        return None
    last = -1
    for i, line in enumerate(lyrics.lines):
        if line.seconds is None:
            continue
        if line.seconds <= position_seconds:
            last = i
        else:
            break
    return last if last >= 0 else None
