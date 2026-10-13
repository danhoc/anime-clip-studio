"""Crash-safe file writes: write temp file, fsync, os.replace."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str | Path, data: bytes, backup: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bak = path.with_name(path.name + ".bak")
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str, backup: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), backup=backup)


def atomic_write_json(path: str | Path, obj: Any, backup: bool = False) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2), backup=backup)


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
