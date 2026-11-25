#!/usr/bin/env python3
"""Mirror Debian's module patch series into the two Manjaro PKGBUILD branches.

Only these target paths are managed:
  * top-level *.patch / *.diff files
  * series.resolved
  * the _debian_patches and sha256sums arrays in PKGBUILD

All other target files (including package configuration files) are left alone.
The source patch order comes from debian/module/debian/patches/series.in.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATCH_DIR = "debian/module/debian/patches"
SERIES_FILE = f"{SOURCE_PATCH_DIR}/series.in"
PATCH_SERIES_TARGET = "series.resolved"

BRANCH_PAIRS = (
    ("340xx/main", "manjaro-340xx-utils"),
    ("390xx/main", "manjaro-390xx-utils"),
)


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}):\n{result.stderr.strip()}"
        )
    return result


def git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return result.stdout


def remote_ref(branch: str) -> str:
    return f"refs/remotes/origin/{branch}"


def read_ref_file(repo: Path, ref: str, path: str) -> bytes:
    return git_bytes(repo, "show", f"{ref}:{path}")


def parse_series(series_text: str) -> list[str]:
    """Resolve the Debian series, retaining #HAS_UVM# entries in-place."""
    patches: list[str] = []
    for line_number, original in enumerate(series_text.splitlines(), start=1):
        entry = original.strip()
        if not entry:
            continue
        if entry.startswith("#HAS_UVM#"):
            entry = entry[len("#HAS_UVM#"):].strip()
        elif entry.startswith("#"):
            continue

        # The current Debian series format uses one patch path per line.
        # Refuse unknown syntax instead of silently creating an incomplete set.
        fields = entry.split()
        if len(fields) != 1:
            raise RuntimeError(
                f"Unsupported series.in syntax on line {line_number}: {original!r}"
            )

        name = fields[0]
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) != 1:
            raise RuntimeError(
                f"Patch entries must be flat filenames (series.in line {line_number}): {name!r}"
            )
        if candidate.suffix.lower() not in {".patch", ".diff"}:
            raise RuntimeError(
                f"Unexpected patch filename extension on series.in line {line_number}: {name!r}"
            )
        patches.append(name)

    if not patches:
        raise RuntimeError("Debian series.in resolved to an empty patch list")
    if len(patches) != len(set(patches)):
        duplicates = sorted({name for name in patches if patches.count(name) > 1})
        raise RuntimeError(f"Duplicate patch entries in Debian series.in: {duplicates}")
    return patches


def shell_array_tokens(body: str, description: str) -> list[str]:
    lexer = shlex.shlex(body, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError as exc:
        raise RuntimeError(f"Cannot parse {description}: {exc}") from exc


def replace_array_block(text: str, variable: str, replacement: str) -> str:
    pattern = re.compile(rf"(?ms)^{re.escape(variable)}=\((.*?)\)")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {variable}=(...) block; found {len(matches)}")
    return pattern.sub(lambda _match: replacement, text, count=1)


def get_array_body(text: str, variable: str) -> tuple[re.Match[str], str]:
    pattern = re.compile(rf"(?ms)^{re.escape(variable)}=\((.*?)\)")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {variable}=(...) block; found {len(matches)}")
    return matches[0], matches[0].group(1)


def update_pkgbuild(pkgbuild_text: str, patches: list[str]) -> str:
    patch_lines = "\n".join(f"    {shlex.quote(name)}" for name in patches)
    patch_array = f"_debian_patches=(\n{patch_lines}\n)"
    updated = replace_array_block(pkgbuild_text, "_debian_patches", patch_array)

    # The PKGBUILD source array defines the number of checksum entries. The
    # Debian patch array is expanded exactly once within source=(...).
    _source_match, source_body = get_array_body(updated, "source")
    source_items = shell_array_tokens(source_body, "source array")
    expansion = "${_debian_patches[@]}"
    if source_items.count(expansion) != 1:
        raise RuntimeError(
            "PKGBUILD source array must contain exactly one "
            '"${_debian_patches[@]}" expansion'
        )
    # Count every literal source entry, regardless of where the patch-array
    # expansion appears. The expansion itself contributes len(patches) URLs.
    static_sources = [item for item in source_items if item != expansion]
    if static_sources.count(PATCH_SERIES_TARGET) != 1:
        raise RuntimeError(
            f"PKGBUILD source array must contain {PATCH_SERIES_TARGET!r} exactly once"
        )

    expected_count = len(static_sources) + len(patches)

    # This package intentionally opts out of checksum validation for EVERY
    # source. Rebuild the array from scratch instead of editing/validating the
    # old array: it may contain stale hashes and even the wrong number of
    # entries after Debian adds, removes, or reorders patches.
    sums_block = "sha256sums=(\n" + "\n".join(
        "            'SKIP'" for _ in range(expected_count)
    ) + "\n)"
    updated = replace_array_block(updated, "sha256sums", sums_block)

    # Verify the generated PKGBUILD invariant before allowing any branch update.
    _generated_match, generated_body = get_array_body(updated, "sha256sums")
    generated_sums = shell_array_tokens(generated_body, "regenerated sha256sums array")
    if generated_sums != ["SKIP"] * expected_count:
        raise RuntimeError(
            "Internal error: regenerated sha256sums must contain exactly "
            f"{expected_count} SKIP entries; got {len(generated_sums)} entries"
        )

    # Keep the explanatory count current if the existing header has one.
    updated = re.sub(
        r"(?m)^(#\s*Átírva: Debian [^\n]*patch-sorozat használatára )\(\d+ patch\)\.$",
        rf"\g<1>({len(patches)} patch).",
        updated,
        count=1,
    )
    updated = re.sub(
        r"(?m)^# Hash-ek:.*$",
        "# Minden source elem checksumja SKIP; az elemszám automatikusan a source tömbhöz igazodik.",
        updated,
        count=1,
    )
    return updated


def patch_file_paths(repo: Path, source_ref: str, patch_names: Iterable[str]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in patch_names:
        source_path = f"{SOURCE_PATCH_DIR}/{name}"
        try:
            content = read_ref_file(repo, source_ref, source_path)
        except RuntimeError as exc:
            raise RuntimeError(
                f"series.in references a patch missing from {source_ref}:{source_path}"
            ) from exc
        files[name] = content
    return files


def prepare_target(
    repo: Path,
    target_worktree: Path,
    source_ref: str,
    target_branch: str,
    patch_names: list[str],
) -> list[str]:
    patch_bytes = patch_file_paths(repo, source_ref, patch_names)
    series_content = "\n".join(patch_names) + "\n"

    pkgbuild_path = target_worktree / "PKGBUILD"
    series_path = target_worktree / PATCH_SERIES_TARGET
    if not pkgbuild_path.is_file():
        raise RuntimeError(f"Target branch {target_branch} has no top-level PKGBUILD")

    original_pkgbuild = pkgbuild_path.read_text(encoding="utf-8")
    updated_pkgbuild = update_pkgbuild(original_pkgbuild, patch_names)

    changed: list[str] = []

    # Remove every old top-level patch payload. The target must represent the
    # Debian series exactly; old Manjaro-only patches are intentionally dropped.
    wanted = set(patch_names)
    for old_patch in list(target_worktree.glob("*.patch")) + list(target_worktree.glob("*.diff")):
        if old_patch.is_file() and old_patch.name not in wanted:
            old_patch.unlink()
            changed.append(old_patch.name + " (deleted)")

    for name, content in patch_bytes.items():
        destination = target_worktree / name
        if not destination.is_file() or destination.read_bytes() != content:
            destination.write_bytes(content)
            changed.append(name + " (added/updated)")

    if not series_path.is_file() or series_path.read_text(encoding="utf-8") != series_content:
        series_path.write_text(series_content, encoding="utf-8", newline="\n")
        changed.append(PATCH_SERIES_TARGET + " (updated)")

    if updated_pkgbuild != original_pkgbuild:
        pkgbuild_path.write_text(updated_pkgbuild, encoding="utf-8", newline="\n")
        changed.append("PKGBUILD (updated)")

    # Defensive invariant: no top-level patch payload remains outside Debian's list.
    leftovers = sorted(
        p.name for p in list(target_worktree.glob("*.patch")) + list(target_worktree.glob("*.diff"))
        if p.is_file() and p.name not in wanted
    )
    if leftovers:
        raise RuntimeError(f"Unexpected patch files remain in target: {leftovers}")

    return changed


def sync_pair(repo: Path, source_branch: str, target_branch: str, dry_run: bool) -> None:
    source_ref = remote_ref(source_branch)
    target_ref = remote_ref(target_branch)

    # Verify both remote refs exist before changing anything.
    run_git(repo, "rev-parse", "--verify", source_ref)
    run_git(repo, "rev-parse", "--verify", target_ref)

    series_text = read_ref_file(repo, source_ref, SERIES_FILE).decode("utf-8")
    patch_names = parse_series(series_text)
    print(f"\n[{source_branch} -> {target_branch}] {len(patch_names)} Debian patches")

    with tempfile.TemporaryDirectory(prefix="debian-patchset-") as temp_root:
        worktree = Path(temp_root) / "target"
        run_git(repo, "worktree", "add", "--detach", str(worktree), target_ref)
        try:
            changed = prepare_target(repo, worktree, source_ref, target_branch, patch_names)
            if not changed:
                print("  No changes; target already matches the Debian patchset.")
                return

            print(f"  Planned changes: {len(changed)}")
            for item in changed:
                print(f"    - {item}")

            if dry_run:
                print("  DRY RUN: no commit or push performed.")
                return

            run_git(worktree, "add", "--all")
            run_git(worktree, "diff", "--cached", "--check")
            run_git(
                worktree,
                "-c", "user.name=github-actions[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit",
                "-m",
                f"Sync {source_branch} Debian patchset into {target_branch}",
            )
            # A normal non-forced push is intentional: concurrent target-branch
            # updates cause a safe rejection instead of overwriting someone else's work.
            run_git(worktree, "push", "origin", f"HEAD:refs/heads/{target_branch}")
            print("  Commit pushed successfully.")
        finally:
            run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPOSITORY_ROOT,
        help="Git repository checkout (default: repository containing this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned updates without committing or pushing",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()

    if not (repo / ".git").exists():
        print(f"ERROR: {repo} is not a Git working tree", file=sys.stderr)
        return 2

    try:
        # Fetch all branches so the exact production refs are used, not the -np
        # inspection branches and not whichever branch happens to be checked out.
        run_git(repo, "fetch", "origin", "+refs/heads/*:refs/remotes/origin/*")
        failures: list[str] = []
        for source_branch, target_branch in BRANCH_PAIRS:
            try:
                sync_pair(repo, source_branch, target_branch, args.dry_run)
            except Exception as exc:  # noqa: BLE001 - keep the two branch pairs independent
                message = f"{source_branch} -> {target_branch}: {exc}"
                failures.append(message)
                print(f"ERROR: {message}", file=sys.stderr)

        if failures:
            print("\nOne or more branch pairs failed:", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001 - fetch/setup failure affects both pairs
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
