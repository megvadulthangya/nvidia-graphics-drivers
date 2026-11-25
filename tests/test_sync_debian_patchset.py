#!/usr/bin/env python3
"""Regression tests for PKGBUILD source/checksum synchronization."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-debian-patchset.py"
SPEC = importlib.util.spec_from_file_location("sync_debian_patchset", SCRIPT)
assert SPEC and SPEC.loader
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


def make_pkgbuild(static_source_count: int, old_checksum_count: int) -> str:
    static_sources = [f"'static-source-{i}.conf'" for i in range(static_source_count - 1)]
    # Ensure series.resolved is present among the static source entries.
    static_sources.insert(0, "'series.resolved'")
    source_body = "\n".join(f"    {item}" for item in static_sources)
    checksum_body = "\n".join(f"    'old-hash-{i}'" for i in range(old_checksum_count))
    return (
        "pkgname=('fixture')\n"
        "_debian_patches=(\n    'old.patch'\n)\n"
        f"source=(\n{source_body}\n    \"${{_debian_patches[@]}}\"\n)\n"
        f"sha256sums=(\n{checksum_body}\n)\n"
    )


class PkgbuildChecksumTests(unittest.TestCase):
    def assert_all_skip(self, updated: str, expected: int) -> None:
        _match, body = SYNC.get_array_body(updated, "sha256sums")
        values = SYNC.shell_array_tokens(body, "sha256sums array")
        self.assertEqual(len(values), expected)
        self.assertEqual(values, ["SKIP"] * expected)

    def test_340xx_real_failure_counts_are_rebuilt(self) -> None:
        # Run log: 8 static sources + 87 Debian patches, old array had 99 hashes.
        patches = [f"debian-{i:03}.patch" for i in range(87)]
        source = make_pkgbuild(static_source_count=8, old_checksum_count=99)
        updated = SYNC.update_pkgbuild(source, patches)
        self.assert_all_skip(updated, 95)
        self.assertIn("_debian_patches=(", updated)
        self.assertIn("debian-086.patch", updated)

    def test_390xx_real_failure_counts_are_rebuilt(self) -> None:
        # Run log: 11 static sources + 83 Debian patches, old array had 100 hashes.
        patches = [f"debian-{i:03}.patch" for i in range(83)]
        source = make_pkgbuild(static_source_count=11, old_checksum_count=100)
        updated = SYNC.update_pkgbuild(source, patches)
        self.assert_all_skip(updated, 94)
        self.assertIn("debian-082.patch", updated)

    def test_reorders_and_removals_change_exact_skip_count(self) -> None:
        original = make_pkgbuild(static_source_count=8, old_checksum_count=95)
        patches = ["new-second.patch", "new-first.patch"]
        updated = SYNC.update_pkgbuild(original, patches)
        self.assert_all_skip(updated, 10)
        self.assertLess(updated.index("new-second.patch"), updated.index("new-first.patch"))

    def test_update_is_idempotent(self) -> None:
        original = make_pkgbuild(static_source_count=8, old_checksum_count=99)
        patches = [f"debian-{i:03}.patch" for i in range(87)]
        once = SYNC.update_pkgbuild(original, patches)
        twice = SYNC.update_pkgbuild(once, patches)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
