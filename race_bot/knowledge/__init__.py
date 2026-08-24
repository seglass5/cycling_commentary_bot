"""Domain knowledge kept as data. Loaded once, cached."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

KNOWLEDGE_DIR = Path(__file__).parent


@cache
def load(name: str) -> dict[str, Any]:
    """Load a YAML knowledge file by stem, e.g. `load("lexicon")`."""
    path = KNOWLEDGE_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no knowledge file named {name!r} in {KNOWLEDGE_DIR}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data
