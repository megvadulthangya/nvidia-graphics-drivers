# Debian → Manjaro patchset synchronizer

This workflow mirrors the Debian module patch series into the matching Manjaro branches:

| Debian source | Manjaro target |
|---|---|
| `340xx/main` | `manjaro-340xx-utils` |
| `390xx/main` | `manjaro-390xx-utils` |

The reduced `340xx/main-np` and `390xx/main-np` branches are for inspection only. The script always reads the production `main` branches.

## Managed target content

The synchronizer reads `debian/module/debian/patches/series.in`, preserving its order and including entries prefixed with `#HAS_UVM#` (with that marker removed). It then:

- copies the listed patch files from Debian into the top level of the matching Manjaro branch;
- removes old top-level `.patch` / `.diff` files that are no longer part of Debian's series;
- regenerates `series.resolved` in Debian order;
- replaces the `_debian_patches` array in `PKGBUILD`;
- regenerates the entire `sha256sums` array with `SKIP` for **every** source entry, including the NVIDIA source archive, helper/configuration files, `series.resolved`, and all Debian patches. The number of `SKIP` entries is calculated automatically as the number of literal `source` entries plus the current Debian patch count. Any old hash values and stale checksum-array lengths are discarded.

All other files in the target branch, including configuration files, are left untouched. Only the patch payloads, `series.resolved`, and the relevant `PKGBUILD` arrays/comments are managed.

## Workflow behavior

`debian-patchset-sync.yml` runs automatically after the existing Salsa synchronization workflow completes successfully. It can also be started manually. Manual runs default to dry-run; disable the `dry_run` input to commit and push.

The script fetches `origin` itself and works from the explicit remote refs. It does not use the `-np` branches for synchronization. Each target update is committed and pushed independently with a normal, non-forced push.

## Local test

From a full clone with access to all four production branches:

```bash
python3 scripts/sync-debian-patchset.py --dry-run
```

Run the regression tests for checksum-array generation and staged whitespace validation:

```bash
python3 -m unittest discover -s tests -v
```

To apply and push the updates:

```bash
python3 scripts/sync-debian-patchset.py
```

The script uses only the Python standard library and the `git` executable.
