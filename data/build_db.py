"""Build the PIN code SQLite database from the IndiaPost/pin dataset.

Usage:
    python build_db.py [/path/to/indiapost-pin] [--out pincodes.db]

If no repo path is given, it shallow-clones https://github.com/IndiaPost/pin
into a temp dir. The output is a SQLite DB with an FTS5 full-text index over
office name / taluk / district / state for fast address -> PIN lookup.
"""
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO_URL = "https://github.com/IndiaPost/pin"

SCHEMA = """
CREATE TABLE IF NOT EXISTS offices (
    id INTEGER PRIMARY KEY,
    pincode TEXT NOT NULL,
    officename TEXT,
    officetype TEXT,
    delivery TEXT,
    division TEXT,
    region TEXT,
    circle TEXT,
    taluk TEXT,
    district TEXT,
    state TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS offices_fts USING fts5(
    officename, taluk, district, state,
    content='offices', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""


def clone_repo(dest):
    print(f"Cloning {REPO_URL} (shallow)...")
    subprocess.run(
        ["git", "clone", "--depth", "1", REPO_URL, dest],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def build_db(csv_dir, out_path):
    if os.path.exists(out_path):
        os.remove(out_path)
    con = sqlite3.connect(out_path)
    con.executescript(SCHEMA)
    rows = []
    files = [f for f in os.listdir(csv_dir) if f.endswith(".csv")]
    print(f"Reading {len(files)} CSV files...")
    for fname in files:
        with open(os.path.join(csv_dir, fname), newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                rows.append((
                    r["pincode"].strip(), r["officename"].strip(),
                    r["officeType"].strip(), r["Deliverystatus"].strip(),
                    r["divisionname"].strip(), r["regionname"].strip(),
                    r["circlename"].strip(), r["Taluk"].strip(),
                    r["Districtname"].strip(), r["statename"].strip(),
                ))
    print(f"Inserting {len(rows)} records...")
    con.executemany(
        "INSERT INTO offices (pincode, officename, officetype, delivery,"
        " division, region, circle, taluk, district, state)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows,
    )
    print("Building full-text index...")
    con.execute(
        "INSERT INTO offices_fts(rowid, officename, taluk, district, state)"
        " SELECT id, officename, taluk, district, state FROM offices"
    )
    con.execute("INSERT INTO offices_fts(offices_fts) VALUES('optimize')")
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM offices").fetchone()[0]
    con.close()
    size_mb = os.path.getsize(out_path) / 1e6
    print(f"Done: {count} records -> {out_path} ({size_mb:.1f} MB)")


def main():
    out = "pincodes.db"
    repo_path = None
    args = sys.argv[1:]
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1]
        args = args[:i] + args[i + 2:]
    if args:
        repo_path = args[0]

    tmpdir = None
    try:
        if repo_path is None:
            tmpdir = tempfile.mkdtemp(prefix="indiapost-pin-")
            clone_repo(tmpdir)
            repo_path = tmpdir
        csv_dir = os.path.join(repo_path, "api", "v01", "csv")
        if not os.path.isdir(csv_dir):
            sys.exit(f"CSV dir not found: {csv_dir}")
        build_db(csv_dir, out)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
