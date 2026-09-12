from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_space(value).lower()).strip()


def slugify(value: str, max_length: int = 80) -> str:
    value = normalize_title(value).replace(" ", "-")
    return (value[:max_length].rstrip("-") or "paper")


def env(name: str | None, default: str | None = None) -> str | None:
    if not name:
        return default
    return os.getenv(name, default)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text file without exposing partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename short: Obsidian paper paths can already be
    # close to Windows' legacy path-length boundary.
    temporary = path.with_name(f".~{uuid.uuid4().hex[:12]}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def extract_json_object(value: str) -> Any:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", value, re.S)
        if not match:
            raise
        return json.loads(match.group(1))
