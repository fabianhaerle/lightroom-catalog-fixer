#!/usr/bin/env python3
"""
fix_missing_photos.py — Find missing images in a Lightroom Classic catalog and
re-link them to files found on disk by matching metadata.

For every photo in the catalog whose file no longer exists on disk, the script
searches the provided search path(s) for a candidate file with the same
metadata:

  1. exact file name (case-insensitive)
  2. file name without extension (same base name, any extension)
  3. capture date/time (from the catalog, or from the XMP sidecar for raw files)
  4. file size (as a weak tie-breaker / sanity check)

Raw files (e.g. .CR2, .NEF, .ARW, .RAF, .DNG, ...) are matched using their
XMP sidecar files (photo.NEF + photo.xmp), because the sidecar carries the
original file name and capture date even after the raw file itself was renamed
or moved.

By default the script runs in DRY-RUN mode: it only reports what it found,
what can be fixed, and what cannot. Pass --apply (or answer a confirmation
prompt) to actually update the catalog database.

The catalog file is never modified unless you explicitly confirm. A backup
copy of the catalog is written next to it before any change.

Usage:
    python fix_missing_photos.py Catalog.lrcat [search_path ...]
    python fix_missing_photos.py Catalog.lrcat D:\\Photos --apply
    python fix_missing_photos.py Catalog.lrcat D:\\Photos --no-dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sqlite3
import struct
import sys
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extensions that Lightroom treats as "raw" and that typically have XMP
# sidecars. Matching is case-insensitive.
RAW_EXTENSIONS = {
    ".3fr", ".ari", ".arw", ".bay", ".cr2", ".cr3", ".crw", ".dcr",
    ".dng", ".erf", ".fff", ".gpr", ".iiq", ".kdc", ".mdc", ".mef",
    ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw",
    ".rw2", ".rwl", ".sr2", ".srf", ".srw", ".x3f",
}

# Extensions Lightroom can catalog directly (non-raw).
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".tif", ".tiff", ".png", ".psd", ".heic", ".heif",
    ".avif", ".webp", ".jxl", ".gif", ".bmp",
}

# All extensions we consider "a photo" when scanning the search path.
KNOWN_EXTENSIONS = RAW_EXTENSIONS | IMAGE_EXTENSIONS

# XMP namespace / property constants
XMP_EXIF_NS = "http://ns.adobe.com/exif/1.0/"
XMP_TIFF_NS = "http://ns.adobe.com/tiff/1.0/"
XMP_PHOTOSHOP_NS = "http://ns.adobe.com/photoshop/1.0/"
XMP_DC_NS = "http://purl.org/dc/elements/1.1/"
XMP_XAP_NS = "http://ns.adobe.com/xap/1.0/"
XMP_XAPMM_NS = "http://ns.adobe.com/xap/1.0/mm/"
XMP_CRS_NS = "http://ns.adobe.com/crs/1.0/"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CatalogPhoto:
    """A photo row from the Lightroom catalog."""

    image_id: int                 # Adobe_images.id_local
    file_id: Optional[int]        # AgLibraryFile.id_local
    folder_id: Optional[int]      # AgLibraryFolder.id_local
    root_folder_id: Optional[int]  # AgLibraryRootFolder.id_local
    filename: str                 # original file name as recorded in catalog
    folder_path: str              # pathFromRoot of the containing folder
    root_path: str                # absolutePath of the root folder
    capture_time: Optional[_dt.datetime]
    file_size: Optional[int]
    rating: Optional[int]
    pick: Optional[int]
    color_labels: Optional[str]
    file_format: Optional[str]
    is_raw: bool
    mod_time: Optional[float] = None   # AgLibraryFile.modTime (epoch seconds)
    # Filled in later:
    old_absolute_path: Optional[str] = None
    exists_on_disk: bool = False
    candidates: List["CandidateFile"] = field(default_factory=list)
    best_candidate: Optional["CandidateFile"] = None

    @property
    def base_name(self) -> str:
        stem, _ = os.path.splitext(self.filename)
        return stem

    @property
    def extension(self) -> str:
        _, ext = os.path.splitext(self.filename)
        return ext.lower()

    @property
    def xmp_sidecar_name(self) -> str:
        return self.base_name + ".xmp"


@dataclass
class CandidateFile:
    """A file found on disk that may match a missing catalog photo."""

    path: str
    size: int
    mtime: float
    # Metadata extracted from the file itself or its XMP sidecar:
    original_filename: Optional[str] = None   # from XMP: photoshop:OriginalDocumentName / xmpMM:DerivedFrom
    capture_time: Optional[_dt.datetime] = None
    source: str = "scan"                      # "scan" | "xmp" | "exif"
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class MatchResult:
    """Outcome of the matching phase for one photo."""

    photo: CatalogPhoto
    status: str                 # "fixed" | "ambiguous" | "no_match" | "ok"
    candidate: Optional[CandidateFile] = None
    message: str = ""


# ---------------------------------------------------------------------------
# Catalog access
# ---------------------------------------------------------------------------


def open_catalog_readonly(catalog_path: str) -> sqlite3.Connection:
    """Open the .lrcat file in read-only mode (safe while LR is running)."""
    uri = "file:" + catalog_path.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def open_catalog_writable(catalog_path: str) -> sqlite3.Connection:
    """Open the .lrcat file for writing. Lightroom must be closed."""
    return sqlite3.connect(catalog_path)


def load_catalog_photos(conn: sqlite3.Connection) -> List[CatalogPhoto]:
    """Load all photos with their file/folder paths from the catalog."""
    query = """
        SELECT
            i.id_local,
            f.id_local,
            fo.id_local,
            rfo.id_local,
            f.idx_filename,
            fo.pathFromRoot,
            rfo.absolutePath,
            i.captureTime,
            i.rating,
            i.pick,
            i.colorLabels,
            i.fileFormat,
            f.modTime,
            f.importHash
        FROM Adobe_images i
        LEFT JOIN AgLibraryFile f   ON i.rootFile = f.id_local
        LEFT JOIN AgLibraryFolder fo ON f.folder = fo.id_local
        LEFT JOIN AgLibraryRootFolder rfo ON fo.rootFolder = rfo.id_local
        ORDER BY i.id_local
    """
    photos: List[CatalogPhoto] = []
    try:
        rows = conn.execute(query).fetchall()
    except sqlite3.OperationalError:
        # Older/simpler catalogs may lack importHash (or modTime); retry
        # without the optional columns.
        query = query.replace(",\n            f.importHash", "") \
                     .replace(",\n            f.modTime", "")
        rows = conn.execute(query).fetchall()
    for row in rows:
        # Pad rows from the fallback query so unpacking always works.
        row = tuple(row) + (None,) * (14 - len(row))
        (image_id, file_id, folder_id, root_folder_id,
         filename, folder_path, root_path,
         capture_time, rating, pick, color_labels,
         file_format, mod_time, import_hash) = row

        filename = filename or ""
        ext = os.path.splitext(filename)[1].lower()
        photos.append(CatalogPhoto(
            image_id=image_id,
            file_id=file_id,
            folder_id=folder_id,
            root_folder_id=root_folder_id,
            filename=filename,
            folder_path=folder_path or "",
            root_path=root_path or "",
            capture_time=parse_lr_datetime(capture_time),
            file_size=parse_import_hash_size(import_hash),
            rating=rating,
            pick=pick,
            color_labels=color_labels,
            file_format=file_format,
            is_raw=ext in RAW_EXTENSIONS,
            mod_time=mod_time,
        ))
    return photos


def parse_import_hash_size(import_hash: Optional[str]) -> Optional[int]:
    """
    Extract the file size from an AgLibraryFile.importHash value.

    Lightroom stores it as ``"<modTime>:<filename>:<fileSize>"``, e.g.
    ``"733325979:DSCF0354.RAF:32539456"``. Returns ``None`` if the hash is
    missing or malformed.
    """
    if not import_hash:
        return None
    parts = import_hash.split(":")
    if len(parts) >= 3:
        try:
            return int(parts[-1])
        except ValueError:
            return None
    return None


def parse_lr_datetime(value: Optional[str]) -> Optional[_dt.datetime]:
    """Parse Lightroom's catalog datetime format (YYYY-MM-DDTHH:MM:SS[.fff])."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def compute_absolute_path(photo: CatalogPhoto) -> Optional[str]:
    """Reconstruct the absolute path of a photo from catalog fields."""
    if not photo.root_path or not photo.filename:
        return None
    parts = [photo.root_path]
    if photo.folder_path:
        parts.append(photo.folder_path.replace("\\", "/").strip("/"))
    parts.append(photo.filename)
    joined = "/".join(p.strip("/") for p in parts if p)
    # Normalise to the platform's separators
    return os.path.normpath(joined.replace("/", os.sep))


# ---------------------------------------------------------------------------
# XMP sidecar parsing
# ---------------------------------------------------------------------------


def parse_xmp_datetime(value: Optional[str]) -> Optional[_dt.datetime]:
    """Parse an XMP date string (ISO 8601, possibly with timezone)."""
    if not value:
        return None
    value = value.strip()
    # Strip timezone suffixes; we only need a naive local comparison.
    if value.endswith("Z"):
        value = value[:-1]
    # Strip +HH:MM / -HH:MM / +HHMM offsets
    if len(value) > 6 and (value[-6] in "+-") and value[-3] == ":":
        value = value[:-6]
    elif len(value) > 5 and (value[-5] in "+-") and value[-2:].isdigit():
        value = value[:-5]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%Y-%m"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def read_xmp_sidecar(xmp_path: str) -> Dict[str, str]:
    """
    Extract useful metadata from an XMP sidecar without external libraries.

    Returns a dict with keys: original_filename, capture_time, metadata_date.
    """
    result: Dict[str, str] = {}
    try:
        with open(xmp_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return result

    def _attr(tag: str, attr: str) -> Optional[str]:
        """Extract attr="value" from the first <tag ...> occurrence."""
        idx = content.find("<" + tag)
        if idx < 0:
            return None
        end = content.find(">", idx)
        if end < 0:
            return None
        tag_text = content[idx:end]
        needle = attr + '="'
        a = tag_text.find(needle)
        if a < 0:
            return None
        a += len(needle)
        b = tag_text.find('"', a)
        if b < 0:
            return None
        return tag_text[a:b]

    def _element(ns: str, prop: str) -> Optional[str]:
        """Extract the text of <ns:prop>value</ns:prop>."""
        tag = "<" + ns + ":" + prop + ">"
        start = content.find(tag)
        if start >= 0:
            start += len(tag)
            end = content.find("</" + ns + ":" + prop + ">", start)
            if end > start:
                return content[start:end].strip()
        return None

    # Original file name: photoshop:OriginalDocumentName or xmpMM:DerivedFrom
    original = (_element("photoshop", "OriginalDocumentName")
                or _element("xmpMM", "DerivedFrom")
                or _attr("photoshop:OriginalDocumentName", "stEvt:parameters"))
    if original:
        result["original_filename"] = os.path.basename(original.strip())

    # Capture time: exif:DateTimeOriginal preferred, then tiff:DateTime, then
    # xmp:MetadataDate / xmp:CreateDate.
    capture = (_element("exif", "DateTimeOriginal")
               or _attr("exif:DateTimeOriginal", "rdf:value")
               or _element("tiff", "DateTime")
               or _element("xmp", "CreateDate")
               or _element("xmp", "MetadataDate"))
    if capture:
        result["capture_time"] = capture

    return result


# ---------------------------------------------------------------------------
# Embedded EXIF parsing (capture time without XMP sidecar)
# ---------------------------------------------------------------------------

# EXIF tag for "DateTimeOriginal" (when the picture was taken) and
# "DateTimeDigitized". Both are ASCII strings formatted "YYYY:MM:DD HH:MM:SS".
EXIF_TAG_DATETIME_ORIGINAL = 0x9003
EXIF_TAG_DATETIME_DIGITIZED = 0x9004
EXIF_TAG_DATETIME = 0x0132

# How many bytes at the start of a file we inspect for an EXIF/TIFF header.
_EXIF_PROBE_BYTES = 4 * 1024 * 1024


def _parse_exif_ascii_date(value: Optional[bytes]) -> Optional[_dt.datetime]:
    """Parse an EXIF ASCII date ('YYYY:MM:DD HH:MM:SS')."""
    if not value:
        return None
    text = value.split(b"\x00")[0].decode("ascii", errors="replace").strip()
    try:
        return _dt.datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _read_exif_ifd(data: bytes, tiff_start: int, ifd_offset: int,
                   byte_order: str) -> Dict[int, object]:
    """
    Read one IFD and return {tag: value}.

    ASCII entries are returned as raw ``bytes``; SHORT/LONG entries are
    returned as ``int`` (needed for the Exif sub-IFD pointer, tag 0x8769).
    """
    entries: Dict[int, object] = {}
    try:
        count = struct.unpack_from(byte_order + "H", data, tiff_start + ifd_offset)[0]
    except struct.error:
        return entries
    base = tiff_start + ifd_offset + 2
    for k in range(min(count, 512)):  # sanity cap
        entry = base + k * 12
        try:
            tag, typ, num = struct.unpack_from(byte_order + "HHI", data, entry)
        except struct.error:
            break
        if typ == 2:  # ASCII
            if num <= 4:
                raw = data[entry + 8:entry + 8 + num]
            else:
                (val_off,) = struct.unpack_from(byte_order + "I", data, entry + 8)
                raw = data[tiff_start + val_off:tiff_start + val_off + num]
            entries[tag] = raw
        elif typ == 3 and num == 1:  # SHORT
            (val,) = struct.unpack_from(byte_order + "H", data, entry + 8)
            entries[tag] = val
        elif typ == 4 and num == 1:  # LONG
            (val,) = struct.unpack_from(byte_order + "I", data, entry + 8)
            entries[tag] = val
    return entries


def extract_exif_capture_time(path: str) -> Optional[_dt.datetime]:
    """
    Read the capture date/time embedded in a file's EXIF metadata.

    Works for JPEG/TIFF-style files (TIFF header at offset 0) and for raw
    formats that embed a TIFF header (CR2, NEF, ARW, RAF, ORF, DNG, ...).
    Returns a naive local datetime or ``None`` if no date could be found.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(_EXIF_PROBE_BYTES)
    except OSError:
        return None

    # Locate the TIFF header ("II*\0" little-endian or "MM\0*" big-endian).
    tiff_start = -1
    byte_order = ""
    for marker, order in ((b"II*\x00", "<"), (b"MM\x00\x2a", ">")):
        idx = data.find(marker)
        if idx >= 0:
            tiff_start, byte_order = idx, order
            break
    if tiff_start < 0:
        return None

    try:
        (ifd0_offset,) = struct.unpack_from(byte_order + "I", data, tiff_start + 4)
        ifd0 = _read_exif_ifd(data, tiff_start, ifd0_offset, byte_order)
    except struct.error:
        return None

    # Preferred: DateTimeOriginal from the Exif sub-IFD.
    exif_ptr = ifd0.get(0x8769)
    if isinstance(exif_ptr, int):
        try:
            exif_ifd = _read_exif_ifd(data, tiff_start, exif_ptr, byte_order)
        except struct.error:
            exif_ifd = {}
        for tag in (EXIF_TAG_DATETIME_ORIGINAL, EXIF_TAG_DATETIME_DIGITIZED):
            parsed = _parse_exif_ascii_date(exif_ifd.get(tag))
            if parsed is not None:
                return parsed

    # Fallbacks: IFD0 DateTime, then the Exif sub-IFD digitized date.
    parsed = _parse_exif_ascii_date(ifd0.get(EXIF_TAG_DATETIME))
    if parsed is not None:
        return parsed
    return None


# ---------------------------------------------------------------------------
# Disk scanning
# ---------------------------------------------------------------------------


class DiskIndex:
    """
    Index of photo files found under the search paths, keyed for fast lookup
    by file name, base name, XMP-recorded original file name, and XMP capture
    time.
    """

    def __init__(self, search_paths: Sequence[str], read_sidecars: bool = True):
        self.by_name: Dict[str, List[CandidateFile]] = {}
        self.by_base: Dict[str, List[CandidateFile]] = {}
        self.by_original_name: Dict[str, List[CandidateFile]] = {}
        self.by_capture_time: Dict[_dt.datetime, List[CandidateFile]] = {}
        self.sidecars_read = 0
        self._scan(search_paths, read_sidecars)

    def _scan(self, search_paths: Sequence[str], read_sidecars: bool) -> None:
        for root_dir in search_paths:
            if not os.path.isdir(root_dir):
                print(f"warning: search path does not exist: {root_dir}",
                      file=sys.stderr)
                continue
            for dirpath, dirnames, filenames in os.walk(root_dir):
                # Skip Lightroom internals and hidden dirs
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d != "Lightroom Catalog Previews.lrdata"]
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in KNOWN_EXTENSIONS:
                        continue
                    full = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    cand = CandidateFile(path=full, size=st.st_size,
                                         mtime=st.st_mtime)
                    base = os.path.splitext(fname)[0].lower()
                    self.by_name.setdefault(fname.lower(), []).append(cand)
                    self.by_base.setdefault(base, []).append(cand)

                    # Raw files: read the XMP sidecar now so renamed files can
                    # be found via OriginalDocumentName / capture time later.
                    if read_sidecars and ext in RAW_EXTENSIONS:
                        self._read_sidecar(cand)

                    # Capture time from embedded EXIF (works for JPEGs and
                    # most raw formats even without an XMP sidecar). This is
                    # essential to tell apart files that share the same name
                    # (e.g. after a camera counter reset).
                    if cand.capture_time is None:
                        cand.capture_time = extract_exif_capture_time(full)
                        if cand.capture_time is not None:
                            cand.capture_time = cand.capture_time.replace(microsecond=0)
                            cand.source = "exif"
                            self.by_capture_time.setdefault(
                                cand.capture_time, []).append(cand)

    def _read_sidecar(self, cand: CandidateFile) -> None:
        base, _ = os.path.splitext(cand.path)
        xmp_path = base + ".xmp"
        if not os.path.isfile(xmp_path):
            return
        meta = read_xmp_sidecar(xmp_path)
        if not meta:
            return
        self.sidecars_read += 1
        if meta.get("original_filename"):
            cand.original_filename = meta["original_filename"]
            cand.source = "xmp"
            key = cand.original_filename.lower()
            self.by_original_name.setdefault(key, []).append(cand)
        if meta.get("capture_time"):
            parsed = parse_xmp_datetime(meta["capture_time"])
            if parsed is not None:
                cand.capture_time = parsed.replace(microsecond=0)
                cand.source = "xmp"
                self.by_capture_time.setdefault(cand.capture_time, []).append(cand)

    def find_by_name(self, filename: str) -> List[CandidateFile]:
        return self.by_name.get(filename.lower(), [])

    def find_by_base(self, base_name: str) -> List[CandidateFile]:
        return self.by_base.get(base_name.lower(), [])

    def find_by_original_name(self, filename: str) -> List[CandidateFile]:
        return self.by_original_name.get(filename.lower(), [])

    def find_by_capture_time(self, when: Optional[_dt.datetime]) -> List[CandidateFile]:
        if when is None:
            return []
        when = when.replace(microsecond=0)
        out: List[CandidateFile] = []
        for offset in (-1, 0, 1):
            out.extend(self.by_capture_time.get(when + _dt.timedelta(seconds=offset), []))
        return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def score_candidate(photo: CatalogPhoto, cand: CandidateFile) -> Tuple[float, List[str]]:
    """
    Score how well a candidate file matches a missing catalog photo.

    Higher is better. Returns (score, reasons).

    Capture time is the strongest identity signal: two photos can share the
    same file name (camera counters reset when the battery is emptied), but
    they were never taken at the same moment. A candidate whose capture time
    contradicts the catalog is therefore rejected outright, while a matching
    capture time earns a large bonus.
    """
    score = 0.0
    reasons: List[str] = []

    cand_base = os.path.splitext(os.path.basename(cand.path))[0].lower()
    cand_ext = os.path.splitext(cand.path)[1].lower()
    photo_base = photo.base_name.lower()
    photo_ext = photo.extension

    # --- 0. capture time conflict check (hard veto) --------------------------
    # If BOTH sides know their capture time and they differ by more than a
    # day, this cannot be the same photo — even if the file name matches
    # exactly. This prevents confusion between same-named files from
    # different shooting sessions (reset camera counters).
    ref_time = photo.capture_time
    cand_time = cand.capture_time
    time_delta: Optional[float] = None
    if ref_time and cand_time:
        time_delta = abs((ref_time.replace(microsecond=0) - cand_time).total_seconds())
        if time_delta > 86400:
            return -1000.0, ["capture time differs by more than a day (rejected)"]

    # --- 1. exact file name -------------------------------------------------
    if cand_base == photo_base:
        if cand_ext == photo_ext:
            score += 100.0
            reasons.append("exact file name match")
        else:
            # Same base name, different extension (e.g. renamed raw format)
            score += 60.0
            reasons.append(f"same base name, different extension ({cand_ext})")

    # --- 2. original file name recorded in XMP sidecar ----------------------
    if cand.original_filename and cand.original_filename.lower() == photo.filename.lower():
        score += 90.0
        reasons.append("XMP sidecar records original file name")

    # --- 3. capture time ----------------------------------------------------
    if time_delta is not None:
        if time_delta == 0:
            score += 50.0
            reasons.append("capture time identical")
        elif time_delta <= 1:
            score += 45.0
            reasons.append("capture time within 1 second")
        elif time_delta <= 60:
            score += 30.0
            reasons.append("capture time within 1 minute")
        elif time_delta <= 3600:
            score += 15.0
            reasons.append("capture time within 1 hour")
        elif time_delta <= 86400:
            score += 5.0
            reasons.append("capture time within 1 day")

    # --- 4. file size -------------------------------------------------------
    if photo.file_size and cand.size:
        if photo.file_size == cand.size:
            score += 20.0
            reasons.append("file size identical")
        else:
            ratio = abs(photo.file_size - cand.size) / max(photo.file_size, 1)
            if ratio > 0.5:
                score -= 10.0
                reasons.append("file size differs by more than 50%")

    # --- 5. raw files should match raw files --------------------------------
    if photo.is_raw and cand_ext in RAW_EXTENSIONS:
        score += 5.0
    elif not photo.is_raw and cand_ext in IMAGE_EXTENSIONS:
        score += 5.0

    return score, reasons


def find_candidates(photo: CatalogPhoto, index: DiskIndex) -> List[CandidateFile]:
    """Return scored candidate files for a missing photo, best first."""
    seen: Dict[str, CandidateFile] = {}

    # 1. Exact file name
    for cand in index.find_by_name(photo.filename):
        seen[cand.path] = cand

    # 2. Same base name, any known extension
    for cand in index.find_by_base(photo.base_name):
        seen.setdefault(cand.path, cand)

    # 3. XMP sidecar records this photo's original file name (renamed files)
    for cand in index.find_by_original_name(photo.filename):
        seen.setdefault(cand.path, cand)

    # 4. Same capture time (±1 s) — catches renamed files without sidecars
    #    only when combined with other signals, and renamed raw files whose
    #    sidecar carries the capture time.
    for cand in index.find_by_capture_time(photo.capture_time):
        seen.setdefault(cand.path, cand)

    scored: List[CandidateFile] = []
    for cand in seen.values():
        score, reasons = score_candidate(photo, cand)
        if score > 0:
            # Copy the candidate: index entries are shared between photos, so
            # per-photo scores/reasons must not leak into other photos'
            # reports (the last photo scored would otherwise win).
            scored.append(replace(cand, score=score, reasons=reasons))

    scored.sort(key=lambda c: c.score, reverse=True)

    # Tie-break equal scores deterministically: prefer the candidate whose
    # capture time is closest to the photo's, then the one with the closest
    # file size. This matters when several files share the same name (reset
    # camera counters) and none of them carries readable metadata.
    if photo.capture_time is not None:
        ref = photo.capture_time.replace(microsecond=0)
        scored.sort(key=lambda c: (
            -c.score,
            abs((c.capture_time - ref).total_seconds())
            if c.capture_time is not None else float("inf"),
            abs(c.size - photo.file_size) if photo.file_size else 0,
            c.path,
        ))
    return scored


# ---------------------------------------------------------------------------
# Reporting / fixing
# ---------------------------------------------------------------------------


def check_existence(photos: List[CatalogPhoto]) -> Tuple[List[CatalogPhoto], List[CatalogPhoto]]:
    """Split photos into (existing, missing) based on the reconstructed path."""
    existing: List[CatalogPhoto] = []
    missing: List[CatalogPhoto] = []
    for photo in photos:
        photo.old_absolute_path = compute_absolute_path(photo)
        photo.exists_on_disk = bool(photo.old_absolute_path and os.path.isfile(photo.old_absolute_path))
        (existing if photo.exists_on_disk else missing).append(photo)
    return existing, missing


def _new_uuid() -> str:
    """Generate an uppercase UUID string like Lightroom uses for id_global."""
    return str(uuid.uuid4()).upper()


def update_catalog_file_location(conn: sqlite3.Connection, photo: CatalogPhoto,
                                 new_path: str) -> None:
    """
    Update the catalog so the photo points to its new location.

    Strategy:
      * If a file with the same name already exists in the target folder row,
        we only update Adobe_images.rootFile to point at it (if needed).
      * Otherwise we update the existing AgLibraryFile row's folder reference
        and/or file name, and move the folder row to a new/existing
        AgLibraryFolder + AgLibraryRootFolder as required.
    """
    new_dir = os.path.dirname(os.path.normpath(new_path))
    new_name = os.path.basename(new_path)

    # --- resolve / create root folder ---------------------------------------
    # Prefer an existing root folder whose absolutePath is a prefix of the
    # new directory. Among matches, prefer the one that yields the SHORTEST
    # relative path (i.e. the closest root), and skip roots that would make
    # the relative path climb above the root (e.g. a root pointing at a
    # subfolder of the target). Only create a new volume-root folder when no
    # existing root can represent the target directory.
    root_row = conn.execute(
        "SELECT id_local, absolutePath FROM AgLibraryRootFolder"
    ).fetchall()

    candidates: List[Tuple[int, str, str]] = []  # (id, norm_path, rel)
    for rid, rpath in root_row:
        rp = os.path.normpath(rpath or "")
        if not rp or rp == os.sep:
            continue
        if new_dir.lower().startswith(rp.lower()):
            rel = os.path.relpath(new_dir, rp)
            if rel.startswith(".."):
                continue
            candidates.append((rid, rp, rel.replace(os.sep, "/")))
    if candidates:
        # Closest root = shortest relative path
        candidates.sort(key=lambda c: len(c[2]))
        root_id, root_prefix, rel_from_root = candidates[0]
    else:
        # No existing root folder matches: create one at the volume root
        # (Lightroom convention: root folders represent volumes/drives).
        drive, _ = os.path.splitdrive(new_dir)
        root_prefix = (drive + os.sep) if drive else new_dir
        max_id = conn.execute(
            "SELECT COALESCE(MAX(id_local), 0) FROM AgLibraryRootFolder"
        ).fetchone()[0]
        root_id = max_id + 1
        conn.execute(
            "INSERT INTO AgLibraryRootFolder (id_local, id_global, name, absolutePath, relativePathFromCatalog) "
            "VALUES (?, ?, ?, ?, ?)",
            (root_id, _new_uuid(), root_prefix, root_prefix, None),
        )
        rel_from_root = os.path.relpath(new_dir, root_prefix).replace(os.sep, "/")
        if rel_from_root == ".":
            rel_from_root = ""

    # --- resolve / create folder row ----------------------------------------
    folder_row = conn.execute(
        "SELECT id_local FROM AgLibraryFolder WHERE rootFolder = ? AND pathFromRoot = ?",
        (root_id, rel_from_root),
    ).fetchone()
    if folder_row:
        folder_id = folder_row[0]
    else:
        max_id = conn.execute(
            "SELECT COALESCE(MAX(id_local), 0) FROM AgLibraryFolder"
        ).fetchone()[0]
        folder_id = max_id + 1
        conn.execute(
            "INSERT INTO AgLibraryFolder (id_local, id_global, pathFromRoot, rootFolder) "
            "VALUES (?, ?, ?, ?)",
            (folder_id, _new_uuid(), rel_from_root, root_id),
        )

    # --- resolve / create file row ------------------------------------------
    file_row = conn.execute(
        "SELECT id_local FROM AgLibraryFile WHERE folder = ? AND idx_filename = ?",
        (folder_id, new_name),
    ).fetchone()
    if file_row:
        file_id = file_row[0]
        # Make sure the image points at this file row
        conn.execute("UPDATE Adobe_images SET rootFile = ? WHERE id_local = ?",
                     (file_id, photo.image_id))
    else:
        # Reuse the existing file row: update folder + name
        if photo.file_id is not None:
            stem, ext = os.path.splitext(new_name)
            try:
                conn.execute(
                    "UPDATE AgLibraryFile SET folder = ?, idx_filename = ?, "
                    "baseName = ?, extension = ?, lc_idx_filename = ?, "
                    "lc_idx_filenameExtension = ? WHERE id_local = ?",
                    (folder_id, new_name, stem, ext.lstrip("."),
                     new_name.lower(), ext.lstrip(".").lower(), photo.file_id),
                )
            except sqlite3.OperationalError:
                # Older catalogs may lack the baseName/extension/lc_* columns;
                # fall back to updating the file name only.
                conn.execute(
                    "UPDATE AgLibraryFile SET folder = ?, idx_filename = ? "
                    "WHERE id_local = ?",
                    (folder_id, new_name, photo.file_id),
                )
            file_id = photo.file_id
        else:
            max_id = conn.execute(
                "SELECT COALESCE(MAX(id_local), 0) FROM AgLibraryFile"
            ).fetchone()[0]
            file_id = max_id + 1
            stem, ext = os.path.splitext(new_name)
            conn.execute(
                "INSERT INTO AgLibraryFile (id_local, id_global, folder, idx_filename, "
                "baseName, extension, lc_idx_filename, lc_idx_filenameExtension, "
                "originalFilename, modTime, modTimeNS, legacyImage, fileSize) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, _new_uuid(), folder_id, new_name, stem,
                 ext.lstrip("."), new_name.lower(), ext.lstrip(".").lower(),
                 new_name, int(os.path.getmtime(new_path)), None, 0,
                 os.path.getsize(new_path)),
            )
        conn.execute("UPDATE Adobe_images SET rootFile = ? WHERE id_local = ?",
                     (file_id, photo.image_id))


def apply_fixes(catalog_path: str, fixes: Sequence[MatchResult],
                backup: bool = True) -> int:
    """Write the re-link updates to the catalog. Returns number of fixes applied."""
    if not fixes:
        return 0

    if backup:
        backup_path = catalog_path + ".backup-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(catalog_path, backup_path)
        print(f"Backup written: {backup_path}")

    conn = open_catalog_writable(catalog_path)
    applied = 0
    try:
        conn.execute("BEGIN")
        for fix in fixes:
            assert fix.candidate is not None
            update_catalog_file_location(conn, fix.photo, fix.candidate.path)
            applied += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return applied


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class RunLogger:
    """
    Tees all console output into a timestamped log file.

    The log file is created next to the catalog (or in the current working
    directory if that fails) with a ``yy-mm-dd-hh-mm-ss`` prefix, e.g.::

        26-09-10-21-34-26_fix_missing_photos.log

    Every line printed through :meth:`print` (or written directly to
    stdout/stderr by this script) is mirrored into the file.
    """

    def __init__(self, log_dir: Optional[str] = None) -> None:
        stamp = _dt.datetime.now().strftime("%y-%m-%d-%H-%M-%S")
        name = f"{stamp}_fix_missing_photos.log"
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, name)
        else:
            path = name
        self.path = path
        self._file = open(path, "a", encoding="utf-8")
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _Tee(self._orig_stdout, self._file)
        sys.stderr = _Tee(self._orig_stderr, self._file)

    def close(self) -> None:
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._file.close()


class _Tee:
    """File-like object that writes to two streams."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for s in self._streams:
            s.write(data)
        return len(data)

    def flush(self) -> None:
        for s in self._streams:
            s.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find missing images in a Lightroom Classic catalog and "
                    "re-link them by matching metadata (file name, capture "
                    "date, XMP sidecars).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("catalog", help="Path to the Lightroom catalog (.lrcat)")
    parser.add_argument("search_paths", nargs="*", default=[],
                        help="Paths to search for the missing images")
    parser.add_argument("--apply", action="store_true",
                        help="Apply fixes without asking for confirmation")
    parser.add_argument("--no-dry-run", dest="no_dry_run", action="store_true",
                        help="Ask for confirmation before applying fixes")
    parser.add_argument("--min-score", type=float, default=60.0,
                        help="Minimum match score to consider a fix")
    parser.add_argument("--no-backup", action="store_true",
                        help="Do not create a backup before modifying the catalog")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show candidate details for every missing photo")
    parser.add_argument("--log-dir", default=None,
                        help="Directory for the run log file (default: next to "
                             "the catalog; falls back to the current directory)")
    return parser


def confirm(message: str) -> bool:
    try:
        answer = input(message.strip() + " [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    catalog_path = os.path.abspath(args.catalog)
    if not os.path.isfile(catalog_path):
        print(f"error: catalog not found: {catalog_path}", file=sys.stderr)
        return 2

    search_paths = [os.path.abspath(p) for p in args.search_paths]
    if not search_paths:
        print("error: at least one search path is required", file=sys.stderr)
        return 2

    # --- start run log -------------------------------------------------------
    # Log next to the catalog by default; fall back to the current directory.
    log_dir = args.log_dir or os.path.dirname(catalog_path)
    try:
        logger = RunLogger(log_dir)
    except OSError:
        logger = RunLogger(".")  # fall back to cwd
    main._active_logger = logger  # type: ignore[attr-defined]
    print(f"Run started {_dt.datetime.now().isoformat(timespec='seconds')}")
    print(f"Catalog:  {catalog_path}")
    print(f"Search:   {', '.join(search_paths)}")
    print(f"Log file: {os.path.abspath(logger.path)}")
    print()

    # --- load catalog --------------------------------------------------------
    try:
        conn = open_catalog_readonly(catalog_path)
    except sqlite3.Error as exc:
        print(f"error: cannot open catalog: {exc}", file=sys.stderr)
        return 2

    try:
        photos = load_catalog_photos(conn)
    finally:
        conn.close()
    print(f"Loaded {len(photos)} photos from catalog.")

    # --- classify ------------------------------------------------------------
    existing, missing = check_existence(photos)
    print(f"  {len(existing)} photos found on disk at their catalog location.")
    print(f"  {len(missing)} photos are MISSING from their catalog location.")

    if not missing:
        print("Nothing to do — no missing photos.")
        return 0

    # --- scan disk -----------------------------------------------------------
    print(f"Scanning {len(search_paths)} search path(s) ...")
    index = DiskIndex(search_paths)
    total_indexed = sum(len(v) for v in index.by_name.values())
    print(f"  indexed {total_indexed} image files.")

    # --- match ---------------------------------------------------------------
    results: List[MatchResult] = []
    for photo in missing:
        candidates = find_candidates(photo, index)
        photo.candidates = candidates
        best = candidates[0] if candidates else None
        if best and best.score >= args.min_score:
            # Guard against same-name confusion: if another candidate scores
            # exactly as high, the match is not unique. Without readable
            # metadata (capture time / size) we cannot tell same-named files
            # apart (e.g. after a camera counter reset), so refuse to guess.
            runner_up = candidates[1] if len(candidates) > 1 else None
            if runner_up is not None and runner_up.score == best.score:
                status = "ambiguous"
                results.append(MatchResult(
                    photo=photo, status="ambiguous", candidate=best,
                    message="several equally good candidates — refusing to "
                            "guess; check capture times manually"))
            else:
                status = "fixed"
                photo.best_candidate = best
                results.append(MatchResult(photo=photo, status="fixed",
                                           candidate=best,
                                           message="; ".join(best.reasons)))
        elif candidates:
            status = "ambiguous"
            results.append(MatchResult(photo=photo, status="ambiguous",
                                       candidate=best,
                                       message="; ".join(best.reasons) if best else ""))
        else:
            status = "no_match"
            results.append(MatchResult(photo=photo, status="no_match"))

    fixable = [r for r in results if r.status == "fixed"]
    ambiguous = [r for r in results if r.status == "ambiguous"]
    no_match = [r for r in results if r.status == "no_match"]

    # --- report --------------------------------------------------------------
    print()
    print("=" * 70)
    print(f"MISSING PHOTOS REPORT  (dry run: {not (args.apply or args.no_dry_run)})")
    print("=" * 70)

    if fixable:
        print(f"\n--- CAN BE FIXED ({len(fixable)}) ---")
        for r in fixable:
            assert r.candidate is not None
            print(f"  {r.photo.filename}")
            print(f"    catalog: {r.photo.old_absolute_path}")
            print(f"    found:   {r.candidate.path}  (score {r.candidate.score:.0f})")
            print(f"    why:     {r.message}")

    if ambiguous:
        print(f"\n--- AMBIGUOUS (below --min-score {args.min_score}) ({len(ambiguous)}) ---")
        for r in ambiguous:
            print(f"  {r.photo.filename}")
            if r.candidate:
                print(f"    best guess: {r.candidate.path}  (score {r.candidate.score:.0f})")
                print(f"    why:        {r.message}")
            if args.verbose:
                for c in r.photo.candidates[:5]:
                    print(f"      candidate: {c.path} (score {c.score:.0f})")

    if no_match:
        print(f"\n--- CANNOT BE FIXED ({len(no_match)}) ---")
        for r in no_match:
            print(f"  {r.photo.filename}  (catalog: {r.photo.old_absolute_path})")

    print()
    print(f"Summary: {len(fixable)} fixable, {len(ambiguous)} ambiguous, "
          f"{len(no_match)} unfixable, {len(existing)} already fine.")

    # --- apply ---------------------------------------------------------------
    apply_requested = args.apply or args.no_dry_run
    if not fixable:
        if apply_requested:
            print("No fixable photos — catalog not modified.")
        return 0

    if not apply_requested:
        print("\nDry run: no changes were made. Re-run with --apply to fix, "
              "or --no-dry-run to be asked for confirmation.")
        return 0

    if args.apply:
        do_apply = True
    else:  # --no-dry-run: ask
        do_apply = confirm(
            f"\nApply {len(fixable)} fix(es) to the catalog? "
            "Make sure Lightroom Classic is CLOSED.")

    if not do_apply:
        print("Aborted — catalog not modified.")
        return 0

    try:
        applied = apply_fixes(catalog_path, fixable, backup=not args.no_backup)
    except sqlite3.Error as exc:
        print(f"error: failed to update catalog: {exc}", file=sys.stderr)
        return 1

    print(f"Done. {applied} photo(s) re-linked in the catalog.")
    print("Re-open the catalog in Lightroom Classic and verify the results.")
    return 0


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point that guarantees the run log is closed on exit."""
    try:
        return main(argv)
    finally:
        logger = getattr(main, "_active_logger", None)
        if logger is not None:
            logger.close()
            main._active_logger = None  # type: ignore[attr-defined]


if __name__ == "__main__":
    sys.exit(run())
