"""Compare fixed-catalog-2 (manually re-imported in Lightroom) vs
fixed-catalog-3 (fixed by the script) to learn how Lightroom itself
represents a re-import / re-link."""

import sqlite3

CATS = {
    "fixed-catalog-2 (manual reimport)":
        r"fixed-catalog-2/exmple-catalog/exmple-catalog.lrcat",
    "fixed-catalog-3 (script fix)":
        r"fixed-catalog-3/exmple-catalog/exmple-catalog.lrcat",
}

QUERIES = [
    ("AgLibraryRootFolder",
     "SELECT id_local, name, absolutePath, relativePathFromCatalog "
     "FROM AgLibraryRootFolder ORDER BY id_local"),
    ("AgLibraryFolder",
     "SELECT id_local, pathFromRoot, rootFolder, parentId "
     "FROM AgLibraryFolder ORDER BY id_local"),
    ("AgLibraryFile",
     "SELECT id_local, folder, idx_filename, originalFilename, importHash, md5 "
     "FROM AgLibraryFile ORDER BY id_local"),
    ("Adobe_images",
     "SELECT id_local, rootFile, captureTime, fileFormat, masterImage "
     "FROM Adobe_images ORDER BY id_local"),
    ("AgLibraryImport",
     "SELECT id_local, name, importDate, numberOfImages "
     "FROM AgLibraryImport ORDER BY id_local"),
    ("AgLibraryImportImage",
     "SELECT * FROM AgLibraryImportImage ORDER BY id_local"),
]


def dump(title: str, path: str) -> dict:
    print("=" * 78)
    print(title)
    print("=" * 78)
    c = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    data = {}
    for name, q in QUERIES:
        try:
            rows = list(c.execute(q))
        except sqlite3.OperationalError as exc:
            print(f"\n--- {name}: ERROR {exc}")
            continue
        data[name] = rows
        print(f"\n--- {name} ({len(rows)} rows) ---")
        for r in rows:
            print(" ", r)
    c.close()
    return data


d2 = dump(*next(iter(CATS.items())))
d3 = dump(list(CATS.items())[1][0], list(CATS.items())[1][1])

print()
print("=" * 78)
print("DIFF SUMMARY")
print("=" * 78)
for name, _ in QUERIES:
    r2, r3 = d2.get(name, []), d3.get(name, [])
    if r2 != r3:
        print(f"\n### {name} differs:")
        only2 = [r for r in r2 if r not in r3]
        only3 = [r for r in r3 if r not in r2]
        if only2:
            print(f"  only in catalog-2 (manual):")
            for r in only2:
                print("   ", r)
        if only3:
            print(f"  only in catalog-3 (script):")
            for r in only3:
                print("   ", r)
    else:
        print(f"\n### {name}: identical")
