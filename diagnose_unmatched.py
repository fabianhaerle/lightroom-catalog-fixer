#!/usr/bin/env python3
"""
diagnose_unmatched.py - Diagnose why photos could not be re-linked.

After running fix_missing_photos.py, some photos remain unmatched (status
"no_match" or "ambiguous").  This script re-runs the same matching logic but
produces a **detailed breakdown** of *why* each photo failed, so you can
decide what to do next:

  - File genuinely not on disk (deleted, not yet recovered, on another drive)
  - File exists but with a different extension (e.g. .CR2 renamed to .DNG)
  - File exists but capture time conflicts (>1 day off - hard veto)
  - File exists but score is below --min-score (weak match only)
  - Multiple equally-good candidates (ambiguous - script refused to guess)
  - Catalog photo has no metadata to match on (no capture time, no file size)

Usage:
    python diagnose_unmatched.py Catalog.lrcat [search_path ...]
    python diagnose_unmatched.py Catalog.lrcat D:\\Photos --verbose
    python diagnose_unmatched.py Catalog.lrcat D:\\Photos --min-score 60

The script is read-only: it never modifies the catalog.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from typing import Dict, List, Optional, Tuple

# Reuse all the matching machinery from the main script.
from fix_missing_photos import (
    CatalogPhoto,
    CandidateFile,
    DiskIndex,
    MatchResult,
    check_existence,
    find_candidates,
    load_catalog_photos,
    open_catalog_readonly,
    parse_import_hash_size,
    ProgressReporter,
    score_candidate,
    RAW_EXTENSIONS,
    IMAGE_EXTENSIONS,
    KNOWN_EXTENSIONS,
)


# ---------------------------------------------------------------------------
# Diagnosis logic
# ---------------------------------------------------------------------------

def diagnose_photo(photo: CatalogPhoto, index: DiskIndex,
                   min_score: float) -> Tuple[str, str, List[CandidateFile]]:
    """
    Return (category, detail, all_candidates) for a single missing photo.

    Categories:
      not_on_disk        - no candidate file found anywhere in search paths
      ext_mismatch        - candidate exists with same base name but different extension
      time_conflict       - candidate found but capture time differs by >1 day (hard veto)
      below_min_score     - best candidate scored >0 but below --min-score
      ambiguous_tie       - two or more candidates tied at the same score
      no_metadata          - photo has no capture time AND no file size; matching is blind
      matched             - would be fixed (should not appear in unmatched set)
    """
    candidates = find_candidates(photo, index)

    if not candidates:
        # No candidate at all.  Try to figure out if the file name exists
        # with a different extension.
        base_matches = index.find_by_base(photo.base_name)
        if base_matches:
            exts = sorted({os.path.splitext(c.path)[1].lower()
                           for c in base_matches})
            return ("ext_mismatch",
                    f"base name '{photo.base_name}' exists as: {', '.join(exts)}",
                    base_matches)
        return ("not_on_disk",
                "no file with this name or base name found in search paths",
                [])

    best = candidates[0]

    # Check for ambiguous tie
    if len(candidates) > 1:
        runner_up = candidates[1]
        if runner_up.score == best.score:
            return ("ambiguous_tie",
                    f"{len(candidates)} candidates tied at score {best.score:.0f}; "
                    f"top 2: {os.path.basename(best.path)}, {os.path.basename(runner_up.path)}",
                    candidates)

    # Check if best was vetoed by capture time
    if best.score <= 0:
        # Re-examine: was it a time conflict?
        time_conflicts = []
        for c in candidates:
            if photo.capture_time and c.capture_time:
                from datetime import timedelta
                delta = abs((photo.capture_time.replace(microsecond=0)
                             - c.capture_time).total_seconds())
                if delta > 86400:
                    time_conflicts.append(
                        f"{os.path.basename(c.path)} "
                        f"(catalog: {photo.capture_time:%Y-%m-%d %H:%M:%S}, "
                        f"file: {c.capture_time:%Y-%m-%d %H:%M:%S}, "
                        f"delta={delta/86400:.1f}d)")
        if time_conflicts:
            return ("time_conflict",
                    f"capture time differs by >1 day: {'; '.join(time_conflicts[:3])}",
                    candidates)
        return ("below_min_score",
                f"best candidate score {best.score:.0f} (all candidates rejected)",
                candidates)

    if best.score < min_score:
        return ("below_min_score",
                f"best candidate '{os.path.basename(best.path)}' "
                f"scored {best.score:.0f} < {min_score:.0f}; "
                f"reasons: {'; '.join(best.reasons)}",
                candidates)

    # Check if photo lacks metadata for matching
    has_capture = photo.capture_time is not None
    has_size = photo.file_size is not None
    if not has_capture and not has_size:
        return ("no_metadata",
                "photo has no capture time and no file size in catalog; "
                "matching relies on file name only",
                candidates)

    return ("matched",
            f"would be fixed: {best.path} (score {best.score:.0f})",
            candidates)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose why photos could not be re-linked by "
                    "fix_missing_photos.py.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("catalog", help="Path to the Lightroom catalog (.lrcat)")
    parser.add_argument("search_paths", nargs="*", default=[],
                        help="Paths to search for the missing images")
    parser.add_argument("--min-score", type=float, default=60.0,
                        help="Minimum match score (same as fix_missing_photos.py)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-photo details for every category")
    parser.add_argument("--no-progress", dest="no_progress", action="store_true",
                        help="Disable the progress bar")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max photos to list per category (use 0 for all)")
    parser.add_argument("--output", "-o", default=None,
                        help="Write the full report to this file (in addition "
                             "to the terminal)")
    args = parser.parse_args()

    catalog_path = args.catalog
    if not os.path.isfile(catalog_path):
        print(f"error: catalog not found: {catalog_path}", file=sys.stderr)
        return 2
    search_paths = args.search_paths or ["."]
    search_paths = [p for p in search_paths if os.path.isdir(p)]
    if not search_paths:
        print("error: no valid search paths provided", file=sys.stderr)
        return 2

    # --- Tee output to file if --output was given --------------------------
    output_file = None
    if args.output:
        output_file = open(args.output, "w", encoding="utf-8")
        _orig_stdout = sys.stdout
        _orig_stderr = sys.stderr

        class _Tee:
            def __init__(self, *streams):
                self._streams = streams
            def write(self, data):
                for s in self._streams:
                    s.write(data)
                return len(data)
            def flush(self):
                for s in self._streams:
                    s.flush()

        sys.stdout = _Tee(_orig_stdout, output_file)
        sys.stderr = _Tee(_orig_stderr, output_file)

    progress = ProgressReporter(enabled=not args.no_progress)

    # --- Load catalog -------------------------------------------------------
    conn = open_catalog_readonly(catalog_path)
    try:
        photos = load_catalog_photos(conn)
    finally:
        conn.close()
    print(f"Loaded {len(photos)} photos from catalog.")

    # --- Classify existing vs missing --------------------------------------
    existing, missing = check_existence(photos, progress=progress)
    print(f"  {len(existing)} photos found on disk at their catalog location.")
    print(f"  {len(missing)} photos are MISSING from their catalog location.")

    if not missing:
        print("Nothing to diagnose - no missing photos.")
        return 0

    # --- Build disk index ---------------------------------------------------
    print(f"Scanning {len(search_paths)} search path(s) ...")
    index = DiskIndex(search_paths, progress=progress)
    total_indexed = sum(len(v) for v in index.by_name.values())
    print(f"  indexed {total_indexed} image files.")

    # --- Diagnose each missing photo ---------------------------------------
    progress.start("Diagnosing", len(missing), unit="photos")
    diagnoses: List[Tuple[CatalogPhoto, str, str, List[CandidateFile]]] = []
    for photo in missing:
        progress.update(1, current=photo.filename)
        category, detail, candidates = diagnose_photo(photo, index, args.min_score)
        diagnoses.append((photo, category, detail, candidates))
    progress.finish()

    # --- Tally by category --------------------------------------------------
    counts = collections.Counter(cat for _, cat, _, _ in diagnoses)
    total = len(diagnoses)

    print()
    print("=" * 70)
    print("UNMATCHED PHOTOS DIAGNOSIS")
    print("=" * 70)
    print(f"\nTotal missing photos: {total}")
    print()

    # Print summary table
    category_labels = {
        "not_on_disk":      "File not on disk at all",
        "ext_mismatch":     "Different file extension",
        "time_conflict":    "Capture time conflicts (>1 day off)",
        "below_min_score":  f"Score below minimum ({args.min_score:.0f})",
        "ambiguous_tie":    "Ambiguous - tied candidates",
        "no_metadata":       "No metadata to match on",
        "matched":           "Would be matched (unexpected)",
    }
    category_order = ["not_on_disk", "ext_mismatch", "time_conflict",
                      "below_min_score", "ambiguous_tie", "no_metadata",
                      "matched"]

    print(f"{'Category':<45} {'Count':>6}  {'%':>5}")
    print("-" * 60)
    for cat in category_order:
        if cat not in counts:
            continue
        n = counts[cat]
        pct = n * 100.0 / total if total else 0
        label = category_labels.get(cat, cat)
        print(f"  {label:<43} {n:>6}  {pct:>4.1f}%")
    print("-" * 60)
    print(f"  {'TOTAL':<43} {total:>6}")

    # --- Per-category details ----------------------------------------------
    limit = args.limit if args.limit > 0 else total

    for cat in category_order:
        items = [(p, d, cands) for p, c, d, cands in diagnoses if c == cat]
        if not items:
            continue
        label = category_labels.get(cat, cat)
        print()
        print(f"--- {label} ({len(items)}) ---")
        for photo, detail, cands in items[:limit]:
            print(f"  {photo.filename}")
            print(f"    catalog path: {photo.old_absolute_path}")
            if photo.capture_time:
                print(f"    capture time: {photo.capture_time:%Y-%m-%d %H:%M:%S}")
            else:
                print(f"    capture time: (none in catalog)")
            if photo.file_size:
                print(f"    file size:    {photo.file_size:,} bytes")
            else:
                print(f"    file size:    (none in catalog)")
            print(f"    diagnosis:    {detail}")
            if args.verbose and cands:
                for c in cands[:5]:
                    ct = c.capture_time.strftime("%Y-%m-%d %H:%M:%S") if c.capture_time else "(none)"
                    print(f"      candidate: {c.path}")
                    print(f"        size: {c.size:,}, capture: {ct}, "
                          f"score: {c.score:.0f}")
        if len(items) > limit:
            print(f"  ... and {len(items) - limit} more (use --limit 0 to see all)")

    # --- Actionable suggestions --------------------------------------------
    print()
    print("=" * 70)
    print("SUGGESTED NEXT STEPS")
    print("=" * 70)
    if counts.get("not_on_disk"):
        print(f"\n  * {counts['not_on_disk']} files are genuinely not in the search")
        print("    paths. Check if they are on a different drive, in a backup")
        print("    that wasn't scanned, or were deleted. Try adding more search paths.")
    if counts.get("ext_mismatch"):
        print(f"\n  * {counts['ext_mismatch']} files exist with a different extension.")
        print("    These may have been converted (e.g. CR2->DNG) or renamed.")
        print("    Re-run fix_missing_photos.py with a lower --min-score, or")
        print("    manually verify and rename if appropriate.")
    if counts.get("time_conflict"):
        print(f"\n  * {counts['time_conflict']} files have a capture time that differs by")
        print("    more than a day. These are likely different photos that happen")
        print("    to share the same name (camera counter reset). Check manually.")
    if counts.get("below_min_score"):
        print(f"\n  * {counts['below_min_score']} files scored below the minimum threshold.")
        print("    Re-run fix_missing_photos.py with --min-score 30 (or lower)")
        print("    to attempt weaker matches. Review the results carefully.")
    if counts.get("ambiguous_tie"):
        print(f"\n  * {counts['ambiguous_tie']} files have multiple equally-good candidates.")
        print("    The script refused to guess. Use --verbose to see the candidates")
        print("    and decide manually, or provide a more specific search path.")
    if counts.get("no_metadata"):
        print(f"\n  * {counts['no_metadata']} photos have no capture time or file size")
        print("    in the catalog. Matching is name-only. If the file was also")
        print("    renamed, it cannot be found automatically. Check if XMP")
        print("    sidecars exist, or locate these files manually.")

    # --- Close output file -------------------------------------------------
    if output_file:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
        output_file.close()
        print(f"\nReport written to: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
