"""Diagnose why Lightroom does not show the moved-images folder content."""
import sqlite3
import sys

CAT = sys.argv[1] if len(sys.argv) > 1 else r"fixed-catalog-2/exmple-catalog/exmple-catalog.lrcat"
c = sqlite3.connect("file:" + CAT.replace("\\", "/") + "?mode=ro", uri=True)

print("=== AgLibraryFolder (all columns) ===")
cols = [r[1] for r in c.execute("PRAGMA table_info(AgLibraryFolder)")]
print("columns:", cols)
for row in c.execute("SELECT * FROM AgLibraryFolder ORDER BY id_local"):
    print(row)

print("\n=== AgLibraryRootFolder (all columns) ===")
cols = [r[1] for r in c.execute("PRAGMA table_info(AgLibraryRootFolder)")]
print("columns:", cols)
for row in c.execute("SELECT * FROM AgLibraryRootFolder ORDER BY id_local"):
    print(row)

print("\n=== AgFolderContent ===")
cols = [r[1] for r in c.execute("PRAGMA table_info(AgFolderContent)")]
print("columns:", cols)
n = c.execute("SELECT COUNT(*) FROM AgFolderContent").fetchone()[0]
print("row count:", n)
for row in c.execute("SELECT * FROM AgFolderContent LIMIT 10"):
    print(row)

print("\n=== Adobe_images (id_local, rootFile) ===")
for row in c.execute("SELECT id_local, rootFile FROM Adobe_images ORDER BY id_local"):
    print(row)

print("\n=== other Folder-related tables row counts ===")
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%Folder%'")]
for t in tables:
    n = c.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    print(f"{t}: {n}")

c.close()
