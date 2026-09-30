from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from urllib.parse import urlparse


def event_id(now: datetime | None = None) -> str:
    instant = now or datetime.now(UTC)
    stamp = instant.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{secrets.token_hex(6)}"


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("A non-empty identifier is required")
    return normalized[:96]


def normalize_remote(remote: str) -> str:
    value = remote.strip().removesuffix(".git")
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"https://{host}/{path}"
    parsed = urlparse(value)
    if parsed.netloc:
        path = parsed.path.strip("/")
        host = parsed.hostname or parsed.netloc
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{host.lower()}/{path}".lower()
    return value.strip("/").lower()


def project_id_from_remote(remote: str) -> str:
    normalized = normalize_remote(remote)
    parts = normalized.split("/")
    return slug("-".join(parts[-2:]) if len(parts) >= 2 else normalized)
