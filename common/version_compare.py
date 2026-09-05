"""Pure semantic version comparison — mirrors adt/lib/version-compare.ts."""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"^\d+$")
_SPLIT = re.compile(r"[.-]")


def _split_segments(version: str) -> list[str]:
    return [part for part in _SPLIT.split(version.strip()) if part]


def _is_numeric(segment: str) -> bool:
    return bool(_NUMERIC.match(segment))


def _compare_segment(left: str | None, right: str | None) -> int:
    a = (left or "").strip() or None
    b = (right or "").strip() or None

    if a is None and b is None:
        return 0
    if a is None:
        if b and _is_numeric(b):
            return 0
        return 1
    if b is None:
        if _is_numeric(a):
            return 0
        return -1

    a_num = _is_numeric(a)
    b_num = _is_numeric(b)

    if a_num and b_num:
        av = int(a)
        bv = int(b)
        if av == bv:
            return 0
        return -1 if av < bv else 1

    if a_num and not b_num:
        return 1
    if not a_num and b_num:
        return -1

    if a == b:
        return 0
    return -1 if a < b else 1


def compare_versions(left: str, right: str) -> int:
    """Return -1 if left < right, 0 if equal, 1 if left > right."""
    if left == right:
        return 0

    a_parts = _split_segments(left)
    b_parts = _split_segments(right)
    width = max(len(a_parts), len(b_parts))

    for index in range(width):
        a_seg = a_parts[index] if index < len(a_parts) else None
        b_seg = b_parts[index] if index < len(b_parts) else None
        result = _compare_segment(a_seg, b_seg)
        if result != 0:
            return result

    return 0


def version_gt(left: str, right: str) -> bool:
    return compare_versions(left, right) > 0


def version_gte(left: str, right: str) -> bool:
    return compare_versions(left, right) >= 0
