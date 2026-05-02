"""Thin wrapper around libsecret for storing Jellyfin tokens."""

from __future__ import annotations

import logging
from typing import Optional

import gi
gi.require_version("Secret", "1")
from gi.repository import GLib, Secret

log = logging.getLogger(__name__)

SCHEMA = Secret.Schema.new(
    "land.rob.Jamjar",
    Secret.SchemaFlags.NONE,
    {
        "server_id": Secret.SchemaAttributeType.STRING,
        "user_id":   Secret.SchemaAttributeType.STRING,
    },
)


def store_token(server_id: str, user_id: str, token: str) -> None:
    """Store a Jellyfin access token in the default keyring."""
    attrs = {"server_id": server_id, "user_id": user_id}
    label = f"Jamjar token for {user_id}@{server_id}"
    Secret.password_store_sync(
        SCHEMA, attrs,
        Secret.COLLECTION_DEFAULT,
        label, token, None,
    )


def lookup_token(server_id: str, user_id: str) -> Optional[str]:
    attrs = {"server_id": server_id, "user_id": user_id}
    try:
        return Secret.password_lookup_sync(SCHEMA, attrs, None)
    except GLib.Error as e:
        log.warning("libsecret lookup failed: %s", e.message)
        return None


def clear_token(server_id: str, user_id: str) -> None:
    attrs = {"server_id": server_id, "user_id": user_id}
    try:
        Secret.password_clear_sync(SCHEMA, attrs, None)
    except GLib.Error as e:
        log.warning("libsecret clear failed: %s", e.message)
