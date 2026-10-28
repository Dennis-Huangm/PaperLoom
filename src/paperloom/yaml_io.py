"""Read fresh configuration content without repeatedly parsing unchanged YAML."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_SAFE_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)


@lru_cache(maxsize=32)
def _parse_yaml(content: str) -> Any:
    return yaml.load(content, Loader=_SAFE_LOADER)


def read_yaml(path: Path) -> Any:
    # Content, rather than timestamps, is the cache key: external edits and
    # same-size replacements must be visible to the very next request.
    # Callers merge and edit these structures, so never lend the cached object.
    return deepcopy(_parse_yaml(path.read_text(encoding='utf-8')))
