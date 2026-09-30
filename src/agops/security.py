from __future__ import annotations

import re
from pathlib import Path

MAX_ENTRY_BYTES = 128 * 1024
PROHIBITED_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
PROHIBITED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|ghp|github_pat)_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_/+=.-]{16,}"
    ),
)


def validate_content(content: str, source_name: str = "entry.md") -> None:
    path = Path(source_name)
    if path.name in PROHIBITED_NAMES or path.suffix.lower() in PROHIBITED_SUFFIXES:
        raise ValueError(f"Refusing prohibited source file: {path.name}")
    if len(content.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise ValueError(f"Entry exceeds {MAX_ENTRY_BYTES} bytes")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise ValueError("Entry appears to contain a credential or private key")
