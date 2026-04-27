"""Filesystem utilities."""

from __future__ import annotations

import os
import re


def sanitize_filename(name: str) -> str:
    """Sanitize a filename: strip path components, limit length, handle edge cases."""
    # Strip NUL bytes and control characters
    name = re.sub(r"[\x00-\x1f]", "", name)

    # Normalize backslashes to forward slashes, then take basename
    name = name.replace("\\", "/")
    name = os.path.basename(name)

    # Remove leading dots to prevent hidden files and traversal
    name = name.lstrip(".")

    if not name:
        return "file"

    # Truncate to 200 chars, preserving extension
    if len(name) > 200:
        base, _, ext = name.rpartition(".")
        if ext and base:
            max_base = 200 - len(ext) - 1  # -1 for the dot
            name = base[:max_base] + "." + ext
        else:
            name = name[:200]

    return name
