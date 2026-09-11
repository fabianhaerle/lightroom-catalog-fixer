# lightroom-catalog-fixer

A Python tool that finds **missing images** in an Adobe Lightroom Classic
catalog (`.lrcat`) and re-links them to files found on disk by matching
metadata — file name, capture date/time, and XMP sidecar contents for raw
files.

Unlike other catalog tools, this one works at the level of **individual
photos**, not whole folders: if you moved, renamed, or reorganized a few
pictures and Lightroom now shows them as missing, this script finds them
again and repairs the catalog entries.

---

## Table of contents

1. [The problem](#the-problem)
2. [Quick start](#quick-start)
3. [Command-line reference](#command-line-reference)
4. [How it works — the algorithm](#how-it-works--the-algorithm)
5. [The Lightroom catalog data format](#the-lightroom-catalog-data-format)
6. [XMP sidecar handling](#xmp-sidecar-handling)
7. [The matching score](#the-matching-score)
8. [Safety model](#safety-model)
9. [Testing](#testing)
10. [Limitations and known issues](#limitations-and-known-issues)
11. [Related projects](#related-projects)

---

## The problem

Lightroom Classic tracks every photo in a SQLite database (the `.lrcat`
file). When a photo's file disappears from its recorded location — because
you moved it in Explorer/Finder, renamed it, restored a backup to a
different path, or a drive letter changed — Lightroom marks it as missing
and offers only two remedies:

- **Locate** the folder manually (one folder at a time, in the UI), or
- re-import the files, losing all edits, flags, keywords, and collection
  membership.

This script automates the repair: it scans a directory tree for the missing
files, matches them against the catalog by metadata, and rewrites the
catalog's internal references so Lightroom finds the photos again — with all
their history intact.

## Quick start

```text
# 1. Dry run (default) — see what's missing and what can be fixed:
python fix_missing_photos.py Catalog.lrcat D:\RecoveredPhotos

# 2. Review the report, then apply:
python fix_missing_photos.py Catalog.lrcat D:\RecoveredPhotos --apply

# 3. Or be asked for confirmation instead:
python fix_missing_photos.py Catalog.lrcat D:\RecoveredPhotos --no-dry-run
```

**Always close Lightroom Classic before using `--apply` or `--no-dry-run`.**

## Command-line reference

```text
usage: fix_missing_photos.py [-h] [--apply] [--no-dry-run]
                             [--min-score MIN_SCORE] [--no-backup]
                             [--verbose] [--no-progress]
                             catalog [search_paths ...]
```

| Argument | Meaning |
|---|---|
| `catalog` | Path to the Lightroom catalog (`.lrcat`) |
| `search_paths` | One or more directories to search for the missing images (searched recursively) |
| `--apply` | Apply fixes immediately without asking |
| `--no-dry-run` | Show the report, then ask "are you sure?" before writing |
| `--min-score N` | Minimum match score for a confident fix (default: `60`) |
| `--no-backup` | Skip the automatic catalog backup before writing |
| `--verbose` / `-v` | Also list candidate files for ambiguous matches |
| `--no-progress` | Disable the progress bar (useful when output is piped or redirected) |
| `--log-dir DIR` | Directory for the run log file (default: next to the catalog) |

Exit codes: `0` success, `1` runtime/write error, `2` usage error (bad
catalog path, no search paths).

### Progress display

Scanning large photo collections can take a while, so the script shows a
live progress bar in the console during its three slow phases — checking
the catalog paths, scanning the search paths, and matching. The bar shows
the phase, a percentage, the item count, and the **file currently being
processed**:

```text
Scanning [====================>                       ]  41%  8,214/20,043 files  D:\RecoveredPhotos\2024\03\DSCF0354.RAF
```

The line is redrawn in place, so it does not clutter the console, and it is
**not** written to the run log (the log only contains the final counts).
Pass `--no-progress` if you pipe or redirect the output; in that case a
short status line is printed at every 10 % milestone instead.

### Run log

Every run writes a log file with a `yy-mm-dd-hh-mm-ss` prefix, created next
to the catalog by default (override with `--log-dir`):

```text
26-09-10-21-47-40_fix_missing_photos.log
```

The log mirrors everything printed to the console — run header (start time,
catalog path, search paths), the full missing-photos report, and the apply
result — so each run leaves a self-contained audit trail. The log is written
even in dry-run mode.

### Example output (dry run)

```text
Loaded 4 photos from catalog.
  2 photos found on disk at their catalog location.
  2 photos are MISSING from their catalog location.
Scanning 1 search path(s) ...
  indexed 2 image files.

======================================================================
MISSING PHOTOS REPORT  (dry run: True)
======================================================================

--- CAN BE FIXED (2) ---
  DSCF0355.RAF
    catalog: G:\...\Work_05\2024\03\DSCF0355.RAF
    found:   G:\...\Work_05\moved-images\DSCF0355.RAF  (score 105)
    why:     exact file name match

Summary: 2 fixable, 0 ambiguous, 0 unfixable, 2 already fine.

Dry run: no changes were made. Re-run with --apply to fix, or --no-dry-run
to be asked for confirmation.
```

---

## How it works — the algorithm

The script runs in five phases:

```
┌─────────────┐   ┌──────────────┐   ┌─────────────┐   ┌──────────┐   ┌───────────┐
│ 1. LOAD     │──▶│ 2. CLASSIFY  │──▶│ 3. SCAN     │──▶│ 4. MATCH │──▶│ 5. REPORT │
│ catalog     │   │ found/missing│   │ search paths│   │ & score  │   │ / APPLY   │
└─────────────┘   └──────────────┘   └─────────────┘   └──────────┘   └───────────┘
```

### Phase 1 — Load the catalog

The catalog is opened **read-only** (`file:...?mode=ro`), so it is safe even
while Lightroom is running. Every photo is loaded with a single SQL join
across the four path tables (see [data format](#the-lightroom-catalog-data-format))
and its absolute path is reconstructed:

```
absolutePath (root folder) + pathFromRoot (folder) + idx_filename (file)
```

### Phase 2 — Classify

Each reconstructed path is checked with `os.path.isfile()`. Photos whose
file exists are "already fine"; the rest are **missing** and enter the
matching phase.

### Phase 3 — Scan the search paths

Every file under the search paths with a known image extension (raw or
non-raw, see the `RAW_EXTENSIONS` / `IMAGE_EXTENSIONS` sets in the source)
is indexed. For **raw** files the XMP sidecar (`photo.NEF` + `photo.xmp`) is
parsed immediately and the candidate is additionally indexed by:

- the **original file name** recorded in the sidecar
  (`photoshop:OriginalDocumentName` / `xmpMM:DerivedFrom`), and
- the **capture time** from the sidecar (`exif:DateTimeOriginal`, …).

This is what makes renamed raw files findable: the sidecar remembers the
name the file had when Lightroom last saw it.

### Phase 4 — Match and score

For each missing photo, candidate files are collected from four lookups:

1. exact file name (case-insensitive)
2. same base name, any known extension
3. XMP sidecar records the photo's original file name
4. same capture time ± 1 second

Each candidate is then scored (see [scoring](#the-matching-score)). The best
candidate above `--min-score` becomes a proposed fix; candidates below the
threshold are reported as *ambiguous*; photos with no candidates at all are
*unfixable*.

### Phase 5 — Report or apply

- **Dry run (default):** prints the full report and exits without touching
  anything.
- **`--apply`:** backs up the catalog, then rewrites the catalog rows inside
  a single transaction (rolled back on any error).
- **`--no-dry-run`:** prints the report, then asks for confirmation before
  doing the same.

---

## The Lightroom catalog data format

A `.lrcat` file is a **standard SQLite 3 database** (WAL mode in recent
Lightroom versions). Adobe does not document the schema; the tables below
were verified against a real Lightroom Classic catalog (124 tables total —
only these four matter here):

```
AgLibraryRootFolder          AgLibraryFolder              AgLibraryFile
┌────────────────────┐  1:N  ┌────────────────────┐  1:N  ┌─────────────────────┐
│ id_local        PK │◀──────│ rootFolder         │◀──────│ folder              │
│ id_global (UUID)   │       │ id_local        PK │       │ id_local         PK │
│ absolutePath       │       │ id_global (UUID)   │       │ id_global (UUID)    │
│ name               │       │ pathFromRoot       │       │ idx_filename        │
└────────────────────┘       └────────────────────┘       │ baseName / extension│
                                                          │ originalFilename    │
                                                          │ modTime / md5 / ... │
                                                          └─────────────────────┘
                                        │ 1:N
                                        ▼
                             ┌────────────────────┐
                             │ Adobe_images       │
                             │ id_local        PK │
                             │ rootFile        FK │──▶ AgLibraryFile.id_local
                             │ captureTime        │
                             │ fileFormat         │
                             │ rating / pick / …  │
                             └────────────────────┘
```

### Column notes (verified against a real catalog)

| Table | Column | Notes |
|---|---|---|
| `AgLibraryRootFolder` | `absolutePath` | Absolute path of a **volume/root**, trailing slash, forward slashes: `G:/Photos/` |
| `AgLibraryFolder` | `pathFromRoot` | Path relative to the root folder, forward slashes, trailing slash: `2024/03/` |
| `AgLibraryFile` | `idx_filename` | File name **with** extension: `DSCF0355.RAF` |
| `AgLibraryFile` | `lc_idx_filename` | Lowercase duplicate of the name (search index) |
| `Adobe_images` | `captureTime` | Text, ISO-ish: `2024-03-28T13:39:39` or with `.%f` milliseconds |
| `Adobe_images` | `fileFormat` | `RAW`, `JPG`, … |
| all `Ag*` tables | `id_global` | **NOT NULL** uppercase UUID string |
| all tables | `id_local` | Integer primary key; new rows must use `MAX(id_local)+1` |

The full path of a photo is therefore:

```
AgLibraryRootFolder.absolutePath + AgLibraryFolder.pathFromRoot + AgLibraryFile.idx_filename
   "G:/Photos/"                   + "2024/03/"                    + "DSCF0355.RAF"
```

### How the fix is written

For each fix, the script (inside one transaction):

1. **Root folder** — finds the longest existing `AgLibraryRootFolder` whose
   `absolutePath` is a prefix of the file's new directory. If none matches,
   a new root folder is created at the volume root (`G:\`), following
   Lightroom's convention that root folders represent volumes.
2. **Folder** — finds or creates the `AgLibraryFolder` row for the new
   directory (`pathFromRoot` relative to the chosen root).
3. **File** — reuses the photo's existing `AgLibraryFile` row and updates
   its `folder` reference (and `idx_filename` if the file was renamed). If
   the target folder already contains a file row with the same name, the
   image's `rootFile` is pointed at that row instead.
4. `Adobe_images.rootFile` is updated to reference the resulting file row.

New rows get fresh uppercase UUIDs for `id_global` and `MAX(id_local)+1`
for `id_local`, mirroring what Lightroom itself writes.

---

## XMP sidecar handling

Raw files keep their edits in `.xmp` sidecars. Because the sidecar travels
with the raw file, it is the most reliable metadata source after a file has
been renamed. The script parses sidecars with plain string operations (no
external dependencies) and extracts:

| XMP property | Used for |
|---|---|
| `photoshop:OriginalDocumentName` | original file name |
| `xmpMM:DerivedFrom` | original file name (fallback) |
| `exif:DateTimeOriginal` | capture time (preferred) |
| `tiff:DateTime` | capture time (fallback) |
| `xmp:CreateDate` / `xmp:MetadataDate` | capture time (fallback) |

Both element form (`<exif:DateTimeOriginal>…</exif:DateTimeOriginal>`) and
attribute form (`<exif:DateTimeOriginal rdf:value="…">`) are handled.
Timezone suffixes (`Z`, `+01:00`) are stripped before comparison.

---

## The matching score

| Signal | Points |
|---|---|
| Exact file name **and** extension | **+100** |
| XMP sidecar records the original file name | **+90** |
| Same base name, different extension | +60 |
| Capture time identical | +50 |
| Capture time within 1 second | +45 |
| Capture time within 1 minute | +30 |
| Capture time within 1 hour | +15 |
| Capture time within 1 day | +5 |
| Capture time differs by more than a day | **rejected (−1000)** |
| File size identical | +20 |
| File size differs by more than 50 % | −10 |
| Raw↔raw or non-raw↔non-raw | +5 |

The default `--min-score 60` means a fix is proposed when there is at least
an exact-name match, or a base-name match plus a close capture-time match.
Raise it (e.g. `--min-score 100`) for stricter matching; lower it if you
want more aggressive proposals (they will be listed as *ambiguous* first).

### Same file name, different dates (reset camera counters)

Cameras that reset their file counter after an empty battery produce
**multiple photos with the same file name** — e.g. two `DSCF0354.RAF` files
shot months apart in different folders. The script never confuses them:

1. **Capture time is read from the files themselves.** Besides XMP sidecars,
   the embedded EXIF (`DateTimeOriginal`) of JPEGs and raw files (CR2, NEF,
   ARW, RAF, ORF, DNG, …) is parsed directly — no sidecar required.
2. **Hard veto.** If a candidate's capture time differs from the catalog's
   by more than a day, it is rejected outright, even if the file name
   matches exactly.
3. **Tie-breaking.** Equal scores are resolved by capture-time distance,
   then file-size distance.
4. **Refuses to guess.** If two candidates still score identically (no
   readable metadata at all), the photo is reported as *ambiguous* instead
   of being re-linked to a possibly wrong file.

A regression test (`test_duplicate_filenames`) covers exactly this case:
two catalog photos named `DSCF0354.RAF` from March and May, both moved —
each is re-linked to the file with the matching capture time, never swapped.

---

## Safety model

- **Read-only analysis.** The catalog is opened with SQLite's `mode=ro`
  URI; nothing can be written during the search/match phases.
- **Dry run by default.** No write happens without `--apply` or an explicit
  "y" at the `--no-dry-run` prompt.
- **Automatic backup.** Before writing, the script copies the catalog to
  `Catalog.lrcat.backup-YYYYmmdd-HHMMSS` next to the original (disable with
  `--no-backup`).
- **Single transaction.** All updates are applied atomically; any error
  rolls back the whole batch.
- **Run log.** Every run writes a timestamped log file
  (`yy-mm-dd-hh-mm-ss_fix_missing_photos.log`) mirroring the console
  output, giving you an audit trail of what was found and changed.
- **Close Lightroom first.** Lightroom caches catalog state in memory; if
  it is running while the catalog is modified, changes can be lost or the
  catalog corrupted.

---

## Testing

```text
python test_fix_missing_photos.py
```

The test suite has three parts:

1. **Synthetic end-to-end test** — builds a minimal catalog from scratch,
   creates a "library" with one existing and three missing photos, and a
   "recovered" folder containing: a moved file (found by name), a renamed
   raw with XMP sidecar (found via `OriginalDocumentName`), and a renamed
   file with no sidecar (correctly reported unfixable). Verifies dry run
   makes no changes and `--apply` writes the expected rows.
2. **Duplicate-name test** — two catalog photos share the file name
   `DSCF0354.RAF` but were shot months apart (reset camera counter). Both
   files were moved; each carries its real EXIF capture time. Verifies each
   photo is re-linked to the file with the *matching* capture time — never
   swapped.
3. **Real-catalog test** — copies `example-data/` (a real Lightroom Classic
   catalog with two deliberately moved pictures), runs the full
   dry-run → apply → verify cycle on the copy, and asserts the original was
   never modified.

`inspect_catalog.py <catalog>` dumps the schema and rows of a catalog for
debugging.

---

## Limitations and known issues

- **Folder-level moves are better fixed in Lightroom.** If an *entire*
  folder moved, use Lightroom's "Find Missing Folder" — it re-links all
  photos in one step. This script is for scattered individual files.
- **Root folder proliferation.** When the fixed location is not under any
  existing root folder, a new root folder (volume) is created. If the whole
  catalog was copied to a new path, the old root folders remain and show as
  stale in Lightroom's folder panel; use Lightroom's "Update Folder
  Location" on the stale root to clean up.
- **EXIF extraction covers common formats.** Capture time is read from
  embedded EXIF (JPEG/TIFF and raw formats that embed a TIFF header: CR2,
  NEF, ARW, RAF, ORF, DNG, …) and from XMP sidecars. Exotic raw variants
  without a TIFF header fall back to name/size signals; same-named files
  without any readable metadata are reported as ambiguous rather than
  guessed.
- **Video files, panoramas, HDR stacks** are not specifically handled;
  they are matched like any other file if their extension is known.
- **Lightroom version compatibility** was verified against a current
  Lightroom Classic catalog (schema with 124 tables). Older catalogs may
  differ slightly; the script fails safely (transaction rollback) if a
  column is missing.

---

## Related projects

- [dedo1911/lightroom-catalog-fixer](https://github.com/dedo1911/lightroom-catalog-fixer) —
  rebuilds corrupted catalogs via SQLite dump/replay (different problem).
- [wrlee/Lightroom-catalog-scripts](https://github.com/wrlee/Lightroom-catalog-scripts) —
  fixes root-folder paths (`AgLibraryRootFolder.absolutePath`), e.g. after
  moving between Windows and macOS (different problem).
- [abay-qkt/lightroom-lrcat-as-sqlite](https://github.com/abay-qkt/lightroom-lrcat-as-sqlite) —
  schema reference queries for the catalog database.

## License

MIT — see [LICENSE].
