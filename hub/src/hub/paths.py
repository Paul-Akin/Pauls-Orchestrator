"""Filesystem anchors.

The hub lives in a monorepo next to ``protocol/`` (the inter-device contract).
Several things need to find the repo root at runtime: the JSON Schemas, the
default config directory, and the ``.env`` file. Resolution order:

1. ``HUB_ROOT`` environment variable (used by the Docker image).
2. Walk up from the current working directory looking for ``protocol/schemas``.
3. Fall back to the location of this source file (editable/workspace install).
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

_MARKER = Path("protocol") / "schemas"


@cache
def repo_root() -> Path:
    env = os.environ.get("HUB_ROOT")
    if env:
        return Path(env).resolve()
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        if (candidate / _MARKER).is_dir():
            return candidate
    # src/hub/paths.py -> parents[0]=src/hub, [1]=src, [2]=hub, [3]=repo
    return Path(__file__).resolve().parents[3]


def schemas_dir() -> Path:
    return repo_root() / _MARKER


def default_config_dir() -> Path:
    return repo_root() / "hub" / "config"
