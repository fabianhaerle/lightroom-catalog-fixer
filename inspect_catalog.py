import sqlite3
import sys

CAT = sys.argv[1] if len(sys.argv) > 1 else r"example-data/exmple-catalog/exmple-catalog.lrcat"
c = sqlite3.connect("file:" + CAT + "?mode=ro", uri=True)

tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(len(tables), "tables")
print(tables)

print("\n--- photos ---")
q = """
SELECT i.id_local, f.idx_filename, fo.pathFromRoot, rfo.absolutePath,
       i.captureTime, i.fileFormat
FROM Adobe_images i
LEFT JOIN AgLibraryFile f ON i.rootFile = f.id_local
LEFT JOIN AgLibraryFolder fo ON f.folder = fo.id_local
LEFT JOIN AgLibraryRootFolder rfo ON fo.rootFolder = rfo.id_local
ORDER BY i.id_local
"""
for row in c.execute(q):
    print(row)

print("\n--- root folders ---")
for row in c.execute("SELECT id_local, name, absolutePath FROM AgLibraryRootFolder"):
    print(row)

print("\n--- folders ---")
for row in c.execute("SELECT id_local, pathFromRoot, rootFolder FROM AgLibraryFolder"):
    print(row)

print("\n--- files ---")
cols = [r[1] for r in c.execute("PRAGMA table_info(AgLibraryFile)")]
print("AgLibraryFile columns:", cols)
for row in c.execute(
    "SELECT id_local, id_global, baseName, extension, folder, idx_filename, "
    "lc_idx_filename, originalFilename, modTime, externalModTime, sidecarExtensions "
    "FROM AgLibraryFile"):
    print(row)

c.close()
