"""Plain data carriers shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Server:
    name: str
    address: str          # http(s)://host:port
    server_id: str
    source: str           # "udp" | "mdns" | "manual"


@dataclass(frozen=True)
class AuthResult:
    access_token: str
    user_id: str
    server_id: str
    server_address: str
    username: str = ""


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    album: str
    album_id: str
    artists: tuple[str, ...]
    artist_ids: tuple[str, ...]
    duration_ticks: int           # 1 tick = 100 ns
    index_number: int | None = None
    parent_index_number: int | None = None
    image_tag: str | None = None
    album_image_tag: str | None = None
    user_data: dict = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return self.duration_ticks / 10_000_000

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    artists: tuple[str, ...]
    artist_ids: tuple[str, ...]
    year: int | None = None
    track_count: int | None = None
    image_tag: str | None = None
    user_data: dict = field(default_factory=dict)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""


@dataclass(frozen=True)
class Artist:
    id: str
    name: str
    album_count: int | None = None
    image_tag: str | None = None
    user_data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    track_count: int | None = None
    image_tag: str | None = None


@dataclass(frozen=True)
class SearchHit:
    item_id: str
    type: str        # "Audio" | "MusicAlbum" | "MusicArtist"
    name: str
    secondary: str = ""
    image_tag: str | None = None


def track_from_json(item: dict) -> Track:
    artists = tuple(a.get("Name", "") for a in item.get("ArtistItems", [])) or \
              tuple(item.get("Artists", []) or [])
    artist_ids = tuple(a.get("Id", "") for a in item.get("ArtistItems", []))
    image_tags = item.get("ImageTags") or {}
    return Track(
        id=item["Id"],
        name=item.get("Name", ""),
        album=item.get("Album", ""),
        album_id=item.get("AlbumId", ""),
        artists=artists,
        artist_ids=artist_ids,
        duration_ticks=int(item.get("RunTimeTicks") or 0),
        index_number=item.get("IndexNumber"),
        parent_index_number=item.get("ParentIndexNumber"),
        image_tag=image_tags.get("Primary"),
        album_image_tag=item.get("AlbumPrimaryImageTag"),
        user_data=item.get("UserData", {}) or {},
    )


def album_from_json(item: dict) -> Album:
    image_tags = item.get("ImageTags") or {}
    artists = tuple(a.get("Name", "") for a in item.get("AlbumArtists", [])) or \
              tuple(item.get("AlbumArtist", "").split(",")) if item.get("AlbumArtist") else tuple()
    artist_ids = tuple(a.get("Id", "") for a in item.get("AlbumArtists", []))
    return Album(
        id=item["Id"],
        name=item.get("Name", ""),
        artists=artists,
        artist_ids=artist_ids,
        year=item.get("ProductionYear"),
        track_count=item.get("ChildCount"),
        image_tag=image_tags.get("Primary"),
        user_data=item.get("UserData", {}) or {},
    )


def artist_from_json(item: dict) -> Artist:
    image_tags = item.get("ImageTags") or {}
    return Artist(
        id=item["Id"],
        name=item.get("Name", ""),
        album_count=item.get("AlbumCount"),
        image_tag=image_tags.get("Primary"),
        user_data=item.get("UserData", {}) or {},
    )


def playlist_from_json(item: dict) -> Playlist:
    image_tags = item.get("ImageTags") or {}
    return Playlist(
        id=item["Id"],
        name=item.get("Name", ""),
        track_count=item.get("ChildCount"),
        image_tag=image_tags.get("Primary"),
    )


def search_hit_from_json(item: dict) -> SearchHit:
    return SearchHit(
        item_id=item["Id"],
        type=item.get("Type", ""),
        name=item.get("Name", ""),
        secondary=item.get("AlbumArtist") or item.get("Album") or "",
        image_tag=item.get("PrimaryImageTag"),
    )
