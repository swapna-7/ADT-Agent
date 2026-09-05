"""Tests for version_compare — mirrors adt/tests/version-compare.test.ts."""

from __future__ import annotations

from version_compare import compare_versions


def test_154_newer_than_152() -> None:
    assert compare_versions("154.0", "152.0.6") == 1


def test_9_10_newer_than_9_9() -> None:
    assert compare_versions("9.10", "9.9") == 1


def test_154_equals_154_0_0() -> None:
    assert compare_versions("154", "154.0.0") == 0


def test_beta_older_than_release() -> None:
    assert compare_versions("154.0-beta", "154.0") == -1
