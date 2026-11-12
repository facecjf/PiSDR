"""Crash-safe file helpers for everything PiSDR keeps under ``~/.config/pisdr``.

A Pi in a box loses power without warning. Writing a file in place can leave it empty or
half-written, which would cost the user their settings or memories. :func:`write_text_atomic`
writes a temporary file next to the target, flushes it to the card and renames it over the
target, so a reader only ever sees the old file or the complete new one.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def write_text_atomic(path: Path | str, text: str, durable: bool = True) -> None:
    """Replace *path* with *text*. Raises OSError; the old file survives any failure.

    *durable* forces the data onto the card before the rename (tens of milliseconds on an
    SD card). Leave it on for anything the user would miss; caches can skip it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if durable:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path | str, data, indent: int | None = 2, durable: bool = True) -> None:
    write_text_atomic(path, json.dumps(data, indent=indent), durable=durable)


def set_aside(path: Path | str) -> Path | None:
    """Rename an unreadable file to ``<name>.corrupt`` so the next save can't destroy it.

    Returns the new path, or None if the file could not be moved.
    """
    path = Path(path)
    target = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, target)
    except OSError as exc:
        log.warning("Could not set %s aside: %s", path, exc)
        return None
    log.warning("%s could not be read; kept as %s", path.name, target)
    return target


def keep_copy(path: Path | str) -> Path | None:
    """Copy a partly unreadable file to ``<name>.corrupt`` before it is rewritten."""
    path = Path(path)
    target = path.with_name(path.name + ".corrupt")
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        log.warning("Could not copy %s: %s", path, exc)
        return None
    log.warning("%s has entries that could not be read; original kept as %s", path.name, target)
    return target


def free_bytes(directory: Path | str) -> int | None:
    """Free space on the filesystem holding *directory* (or its nearest existing parent)."""
    path = Path(directory)
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None
