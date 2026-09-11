#!/usr/bin/env python3
"""
End-to-end test for fix_missing_photos.py.

Builds a synthetic Lightroom catalog (minimal schema with the tables/columns
the script touches), a "library" with one existing photo and three missing
ones, and a "recovered" folder containing the moved/renamed files. Then runs
the script's main() in dry-run and apply mode and checks the results.
"""

import datetime as dt
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "fix_missing_photos.py")

SCHEMA = """
CREATE TABLE AgLibraryRootFolder (
    id_local INTEGER PRIMARY KEY,
    id_global TEXT,
    name TEXT,
    absolutePath TEXT,
    relativePathFromCatalog TEXT
);
CREATE TABLE AgLibraryFolder (
    id_local INTEGER PRIMARY KEY,
    id_global TEXT,
    pathFromRoot TEXT,
    rootFolder INTEGER,
    dateCreated TEXT
);
CREATE TABLE AgLibraryFile (
    id_local INTEGER PRIMARY KEY,
    id_global TEXT,
    folder INTEGER,
    idx_filename TEXT,
    baseName TEXT,
    extension TEXT,
    lc_idx_filename TEXT,
    lc_idx_filenameExtension TEXT,
    originalFilename TEXT,
    modTime INTEGER,
    modTimeNS INTEGER,
    legacyImage INTEGER,
    fileSize INTEGER
);
CREATE TABLE Adobe_images (
    id_local INTEGER PRIMARY KEY,
    id_global TEXT,
    rootFile INTEGER,
    captureTime TEXT,
    fileFormat TEXT,
    rating INTEGER,
    pick INTEGER,
    colorLabels TEXT
);
"""


def make_catalog(path: str, library_dir: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.execute(
        "INSERT INTO AgLibraryRootFolder VALUES (1, NULL, 'Library', ?, NULL)",
        (library_dir + os.sep,),
    )
    conn.execute(
        "INSERT INTO AgLibraryFolder VALUES (1, NULL, '', 1, '2020-01-01T00:00:00')"
    )

    now = dt.datetime.now()
    files = [
        # (file_id, filename, exists_on_disk, capture, size, fmt)
        (1, "IMG_0001.CR2", True,  "2021-05-01T10:00:00", 25_000_000, "RAW"),
        (2, "IMG_0002.CR2", False, "2021-05-01T10:05:00", 24_900_000, "RAW"),
        (3, "IMG_0003.NEF", False, "2021-06-15T18:30:00", 30_000_000, "RAW"),
        (4, "IMG_0004.JPG", False, "2021-07-04T12:00:00",  4_000_000, "JPEG"),
    ]
    for fid, fname, exists, capture, size, fmt in files:
        stem, ext = os.path.splitext(fname)
        # Columns: id_local, id_global, folder, idx_filename, baseName,
        # extension, lc_idx_filename, lc_idx_filenameExtension,
        # originalFilename, modTime, modTimeNS, legacyImage, fileSize
        conn.execute(
            "INSERT INTO AgLibraryFile VALUES (?, NULL, 1, ?, ?, ?, ?, ?, ?, "
            "?, NULL, 0, ?)",
            (fid, fname, stem, ext.lstrip("."), fname.lower(),
             ext.lstrip(".").lower(), fname, int(now.timestamp()), size),
        )
        conn.execute(
            "INSERT INTO Adobe_images VALUES (?, NULL, ?, ?, ?, 0, 0, NULL)",
            (fid, fid, capture, fmt),
        )
        if exists:
            with open(os.path.join(library_dir, fname), "wb") as fh:
                fh.write(b"\0" * size)
    conn.commit()
    conn.close()


def make_recovered(recovered_dir: str) -> None:
    os.makedirs(recovered_dir, exist_ok=True)
    now = dt.datetime.now()

    # Case 1: IMG_0002.CR2 moved to a subfolder, name unchanged.
    sub = os.path.join(recovered_dir, "2021", "May")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "IMG_0002.CR2"), "wb") as fh:
        fh.write(b"\0" * 24_900_000)

    # Case 2: IMG_0003.NEF renamed to DSC_9876.NEF but XMP sidecar kept.
    with open(os.path.join(recovered_dir, "DSC_9876.NEF"), "wb") as fh:
        fh.write(b"\0" * 30_000_000)
    with open(os.path.join(recovered_dir, "DSC_9876.xmp"), "w",
              encoding="utf-8") as fh:
        fh.write("""<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:exif="http://ns.adobe.com/exif/1.0/">
   <photoshop:OriginalDocumentName>IMG_0003.NEF</photoshop:OriginalDocumentName>
   <exif:DateTimeOriginal>2021-06-15T18:30:00</exif:DateTimeOriginal>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
""")

    # Case 3: IMG_0004.JPG renamed to vacation.jpg (no sidecar) — should be
    # ambiguous, not confidently fixable.
    with open(os.path.join(recovered_dir, "vacation.jpg"), "wb") as fh:
        fh.write(b"\0" * 4_000_000)


# ---------------------------------------------------------------------------
# Minimal real EXIF blob builders for the duplicate-name regression test.
# ---------------------------------------------------------------------------

def build_exif_bytes(capture: dt.datetime, little_endian: bool = True) -> bytes:
    """Build a minimal TIFF/EXIF block containing DateTimeOriginal."""
    order = b"II" if little_endian else b"MM"
    fmt = "<" if little_endian else ">"
    date_str = capture.strftime("%Y:%m:%d %H:%M:%S").encode("ascii") + b"\x00"

    # Layout: header(8) | IFD0(2+2*12+4) | ExifIFD(2+2*12+4) | date bytes
    ifd0_off = 8
    exif_ifd_off = ifd0_off + 2 + 12 + 4
    date_off = exif_ifd_off + 2 + 12 + 4
    date_len = len(date_str)

    out = bytearray()
    out += order + b"\x2a\x00" if little_endian else order + b"\x00\x2a"
    out += struct.pack(fmt + "I", ifd0_off)
    # IFD0: one entry -> ExifIFD pointer
    out += struct.pack(fmt + "H", 1)
    out += struct.pack(fmt + "HHI", 0x8769, 4, 1) + struct.pack(fmt + "I", exif_ifd_off)
    out += struct.pack(fmt + "I", 0)  # next IFD = none
    # Exif IFD: one entry -> DateTimeOriginal (ASCII, count = date_len)
    out += struct.pack(fmt + "H", 1)
    out += struct.pack(fmt + "HHI", 0x9003, 2, date_len)
    if date_len <= 4:
        out += date_str.ljust(4, b"\x00")
    else:
        out += struct.pack(fmt + "I", date_off)
    out += struct.pack(fmt + "I", 0)  # next IFD = none
    assert len(out) == date_off, (len(out), date_off)
    out += date_str
    return bytes(out)


def write_jpeg_with_exif(path: str, capture: dt.datetime, size: int) -> None:
    """Write a minimal JPEG (SOI/APP0/APP1/EOI) with EXIF capture time."""
    exif = build_exif_bytes(capture)
    app1_payload = b"Exif\x00\x00" + exif
    app1 = b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
    data = b"\xff\xd8" + app1 + b"\xff\xdb" + b"\x00" * 64 + b"\xff\xd9"
    # Pad to the requested size so file sizes differ between candidates.
    data += b"\0" * max(0, size - len(data))
    with open(path, "wb") as fh:
        fh.write(data)


def run(args, expect_rc=0):
    proc = subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True,
    )
    if proc.returncode != expect_rc:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise AssertionError(f"exit code {proc.returncode} != {expect_rc}")
    return proc.stdout


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="lrcat-test-")
    try:
        catalog = os.path.join(tmp, "test.lrcat")
        library = os.path.join(tmp, "library")
        recovered = os.path.join(tmp, "recovered")
        os.makedirs(library, exist_ok=True)
        make_catalog(catalog, library)
        make_recovered(recovered)

        # ---------------- dry run ----------------
        out = run([catalog, recovered])
        assert "MISSING PHOTOS REPORT" in out
        assert "dry run: True" in out
        assert "3 photos are MISSING" in out
        assert "CAN BE FIXED (2)" in out, out
        assert "IMG_0002.CR2" in out
        assert "IMG_0003.NEF" in out
        # vacation.jpg shares no metadata with IMG_0004.JPG (no sidecar,
        # different name) -> correctly reported as unfixable
        assert "CANNOT BE FIXED (1)" in out, out
        assert "IMG_0004.JPG" in out
        assert "no changes were made" in out
        print("--- dry run output OK ---")
        print(out)

        # catalog untouched after dry run
        conn = sqlite3.connect(catalog)
        n = conn.execute(
            "SELECT COUNT(*) FROM AgLibraryFile WHERE folder != 1"
        ).fetchone()[0]
        conn.close()
        assert n == 0, "dry run modified the catalog!"

        # ---------------- apply ----------------
        out = run([catalog, recovered, "--apply"])
        assert "re-linked" in out, out
        print("--- apply output OK ---")
        print(out)

        conn = sqlite3.connect(catalog)
        # IMG_0002 must now point to recovered/2021/May
        row = conn.execute(
            "SELECT fo.pathFromRoot, f.idx_filename FROM Adobe_images i "
            "JOIN AgLibraryFile f ON i.rootFile = f.id_local "
            "JOIN AgLibraryFolder fo ON f.folder = fo.id_local "
            "WHERE f.idx_filename = 'IMG_0002.CR2'"
        ).fetchone()
        assert row is not None, "IMG_0002.CR2 row lost"
        assert row[0].replace("\\", "/").rstrip("/").endswith("2021/May"), row
        # IMG_0003 must be re-linked to DSC_9876.NEF
        row = conn.execute(
            "SELECT f.idx_filename FROM Adobe_images i "
            "JOIN AgLibraryFile f ON i.rootFile = f.id_local "
            "WHERE f.originalFilename = 'IMG_0003.NEF' OR f.idx_filename = 'DSC_9876.NEF'"
        ).fetchone()
        assert row is not None and row[0] == "DSC_9876.NEF", row
        conn.close()

        # backup exists
        backups = [f for f in os.listdir(tmp) if f.startswith("test.lrcat.backup-")]
        assert backups, "no backup created"

        print("\nALL TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_duplicate_filenames() -> int:
    """
    Regression test: two catalog photos share the same file name but were
    shot at different times (camera counter reset after empty battery) and
    live in different folders. Both files were moved elsewhere.

    The script must re-link each photo to the file with the MATCHING capture
    time — never swap them.
    """
    tmp = tempfile.mkdtemp(prefix="lrcat-dup-")
    try:
        catalog = os.path.join(tmp, "dup.lrcat")
        library = os.path.join(tmp, "library")
        recovered = os.path.join(tmp, "recovered")
        os.makedirs(library, exist_ok=True)

        # Build the catalog: two photos named DSCF0354.RAF in different
        # folders, captured 2 months apart.
        conn = sqlite3.connect(catalog)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO AgLibraryRootFolder VALUES (1, NULL, 'Library', ?, NULL)",
            (library + os.sep,),
        )
        conn.execute(
            "INSERT INTO AgLibraryFolder VALUES (1, NULL, '', 1, NULL)")
        conn.execute(
            "INSERT INTO AgLibraryFolder VALUES (2, NULL, '2024/03/', 1, NULL)")
        conn.execute(
            "INSERT INTO AgLibraryFolder VALUES (3, NULL, '2024/05/', 1, NULL)")
        now = int(dt.datetime.now().timestamp())
        # (file_id, folder_id, capture, size)
        photos = [
            (1, 2, "2024-03-28T13:39:39", 32_539_456),   # March shot
            (2, 3, "2024-05-10T09:15:00", 31_111_111),   # May shot (same name!)
        ]
        for fid, folder_id, capture, size in photos:
            conn.execute(
                "INSERT INTO AgLibraryFile VALUES (?, NULL, ?, 'DSCF0354.RAF', "
                "'DSCF0354', 'RAF', 'dscf0354.raf', 'raf', 'DSCF0354.RAF', "
                "?, NULL, 0, ?)",
                (fid, folder_id, now, size),
            )
            conn.execute(
                "INSERT INTO Adobe_images VALUES (?, NULL, ?, ?, 'RAW', 0, 0, NULL)",
                (fid, fid, capture),
            )
        conn.commit()
        conn.close()

        # Both files were moved into "recovered" — same names, different
        # dates, each carrying its real EXIF capture time.
        os.makedirs(recovered, exist_ok=True)
        os.makedirs(os.path.join(recovered, "sub"), exist_ok=True)
        write_jpeg_with_exif(
            os.path.join(recovered, "DSCF0354.RAF"),
            dt.datetime(2024, 5, 10, 9, 15, 0), 31_111_111)
        write_jpeg_with_exif(
            os.path.join(recovered, "sub", "DSCF0354.RAF"),
            dt.datetime(2024, 3, 28, 13, 39, 39), 32_539_456)

        # ---------------- dry run ----------------
        out = run([catalog, recovered])
        assert "2 photos are MISSING" in out, out
        assert "CAN BE FIXED (2)" in out, out
        assert "capture time identical" in out, out
        assert "no changes were made" in out, out
        print("--- duplicate-name dry run OK ---")
        print(out)

        # ---------------- apply ----------------
        out = run([catalog, recovered, "--apply"])
        assert "2 photo(s) re-linked" in out, out

        # ---------------- verify: no swap ----------------
        conn = sqlite3.connect(catalog)
        rows = dict(conn.execute(
            "SELECT i.captureTime, fo.pathFromRoot FROM Adobe_images i "
            "JOIN AgLibraryFile f ON i.rootFile = f.id_local "
            "JOIN AgLibraryFolder fo ON f.folder = fo.id_local"
        ).fetchall())
        conn.close()
        # pathFromRoot is relative to the chosen root folder (here the volume
        # root), so compare folder suffixes.
        march_folder = rows["2024-03-28T13:39:39"].replace("\\", "/").rstrip("/")
        may_folder = rows["2024-05-10T09:15:00"].replace("\\", "/").rstrip("/")
        assert march_folder.endswith("recovered/sub"), (rows, "March photo matched wrong file!")
        assert may_folder.endswith("recovered") and not may_folder.endswith("recovered/sub"), (rows, "May photo matched wrong file!")

        # The re-linked file rows must carry consistent name columns
        # (baseName/extension/lc_idx_filename updated together with
        # idx_filename — see update_catalog_file_location).
        conn = sqlite3.connect(catalog)
        name_rows = conn.execute(
            "SELECT idx_filename, baseName, extension, lc_idx_filename "
            "FROM AgLibraryFile"
        ).fetchall()
        conn.close()
        for idx_name, base, ext, lc_name in name_rows:
            stem, ext_raw = os.path.splitext(idx_name)
            assert base == stem, (name_rows, "baseName not updated on rename")
            assert ext == ext_raw.lstrip("."), (name_rows, "extension not updated on rename")
            assert lc_name == idx_name.lower(), (name_rows, "lc_idx_filename not updated on rename")
        print("duplicate-name verification OK (no swap)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_catalog() -> int:
    """Test against the real example catalog in example-data/ (on a copy)."""
    here = os.path.dirname(os.path.abspath(__file__))
    real_catalog = os.path.join(
        here, "example-data", "exmple-catalog", "exmple-catalog.lrcat")
    moved_dir = os.path.join(
        here, "example-data", "exmple-catalog", "Work_05", "moved-images")

    if not os.path.isfile(real_catalog):
        print("real example catalog not present — skipping real-catalog test")
        return 0

    tmp = tempfile.mkdtemp(prefix="lrcat-real-")
    try:
        catalog = os.path.join(tmp, "real.lrcat")
        shutil.copy2(real_catalog, catalog)

        # ---------------- dry run ----------------
        out = run([catalog, moved_dir])
        assert "2 photos are MISSING" in out, out
        assert "CAN BE FIXED (2)" in out, out
        assert "DSCF0355.RAF" in out
        assert "IMG_6302.JPEG" in out
        assert "no changes were made" in out
        print("--- real catalog dry run OK ---")

        # ---------------- apply ----------------
        out = run([catalog, moved_dir, "--apply"])
        assert "2 photo(s) re-linked" in out, out
        print("--- real catalog apply OK ---")

        # ---------------- verify ----------------
        conn = sqlite3.connect(catalog)
        rows = dict(conn.execute(
            "SELECT f.idx_filename, fo.pathFromRoot FROM Adobe_images i "
            "JOIN AgLibraryFile f ON i.rootFile = f.id_local "
            "JOIN AgLibraryFolder fo ON f.folder = fo.id_local"
        ).fetchall())
        conn.close()
        assert rows["DSCF0354.RAF"].replace("\\", "/") == "2024/03/", rows
        assert rows["IMG_6313.JPEG"].replace("\\", "/") == "2024/03/", rows
        assert rows["DSCF0355.RAF"].replace("\\", "/") == "moved-images/", rows
        assert rows["IMG_6302.JPEG"].replace("\\", "/") == "moved-images/", rows

        # original catalog untouched
        conn = sqlite3.connect(
            "file:" + real_catalog.replace("\\", "/") + "?mode=ro", uri=True)
        n = conn.execute(
            "SELECT COUNT(*) FROM AgLibraryFolder WHERE pathFromRoot = 'moved-images'"
        ).fetchone()[0]
        conn.close()
        assert n == 0, "original catalog was modified!"

        print("real catalog verification OK")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    rc = test_duplicate_filenames() or rc
    rc = test_real_catalog() or rc
    print("\nALL TESTS PASSED (including real catalog)")
    sys.exit(rc)
