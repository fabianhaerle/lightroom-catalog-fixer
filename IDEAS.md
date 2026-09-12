# Ideas: Path Remapping Tool for Multi-Laptop Catalog Sync

## The Problem

A `.lrcat` stores absolute paths as `AgLibraryRootFolder.absolutePath` +
`AgLibraryFolder.pathFromRoot` + `AgLibraryFile.idx_filename`. When the same
catalog is opened on a laptop with a different path structure (different
username, different mount point, different drive letter), **every** photo shows
as missing — even though the files exist, just at a different root.

---

## Idea 1: Simple Path Remap (one-shot)

A CLI tool that takes a mapping of old-prefix → new-prefix and rewrites
`AgLibraryRootFolder.absolutePath` (and optionally `AgLibraryFolder.pathFromRoot`)
rows in bulk.

```
python remap_paths.py catalog.lrcat \
    --map /Users/fabian/Photos/ /Users/john/Pictures/ \
    --apply
```

- Dry-run by default (consistent with existing tool's safety model)
- Reuses `open_catalog_readonly` / `open_catalog_writable` / `apply_fixes` backup logic
- Checks that the new paths actually exist on disk before writing
- Handles multiple root folders in one pass
- Could also remap sub-paths (e.g. `/2024/` → `/Archive/2024/`) if folder structures also differ

**Pros:** Minimal, fast, easy to understand.
**Cons:** Manual — you have to know the exact mappings.

---

## Idea 2: Named Path Profiles (switch command)

Store a JSON sidecar file next to the catalog (e.g.
`catalog-path-profiles.json`) containing named profiles:

```json
{
  "profiles": {
    "macbook-fabian": { "/Volumes/Photos/": "/Users/fabian/Photos/" },
    "macbook-pro": { "/Volumes/Photos/": "/Users/fabian/Pictures/Lightroom/" },
    "windows-desktop": { "/Volumes/Photos/": "G:/Photos/" }
  },
  "active": "macbook-fabian"
}
```

Then a `switch` command rewrites all root folder paths for the target profile:

```
python remap_paths.py catalog.lrcat switch macbook-pro --apply
python remap_paths.py catalog.lrcat list-profiles
python remap_paths.py catalog.lrcat add-profile windows-desktop ...
```

**Pros:** Repeatable, designed for the "sync catalog via cloud, open on different
machines" workflow.
**Cons:** Requires maintaining the profile file; the catalog itself can't be
open in Lightroom during a switch.

---

## Idea 3: Auto-Detect Path Remap (scan-based)

Leverage the existing `DiskIndex` / scanning machinery. Instead of matching
individual photos, it:

1. Reads all root folders from the catalog
2. For each missing root, scans candidate search paths for a **folder
   fingerprint** — e.g. the set of subfolder names and file counts under the
   old root
3. When a match is found, rewrites the root folder's `absolutePath`

```
python remap_paths.py catalog.lrcat --auto-detect /Users/fabian/Pictures /Users/john/Photos --apply
```

**Pros:** No manual mapping needed; works even when you don't know the exact new
path.
**Cons:** Slower (full disk scan); folder fingerprinting can be ambiguous.

---

## Idea 4: Hybrid (recommended)

Combine all three into a single tool:

- **`remap`** — explicit old→new prefix mapping (Idea 1)
- **`switch <profile>`** — use a named profile from the sidecar JSON (Idea 2)
- **`detect`** — auto-find new paths by scanning (Idea 3)
- **`snapshot`** — capture the current path configuration as a new profile (so
  after you fix paths manually in Lightroom, you can save them for future
  switching)
- **`status`** — show which root folders are valid vs missing on the current
  machine

This reuses the existing `CatalogPhoto` loading, `open_catalog_*`, backup, and
`ProgressReporter` infrastructure almost directly.

---

## Key Design Decisions to Make

| Question | Options |
|---|---|
| **Granularity** | Root-folder level only (fast, covers the common case) vs. folder-level (handles differing sub-structures) |
| **Profile storage** | JSON sidecar next to catalog vs. a central config in `~/.config/lr-tools/` |
| **Sync workflow** | Does the catalog get synced via cloud (Dropbox/iCloud)? If so, the profile file should travel *with* the catalog |
| **Path separator** | Normalize `/` vs `\` cross-platform? Or keep it macOS-only? |
| **When to run** | Manual before opening LR, or a wrapper script / LaunchAgent that auto-switches on login? |
