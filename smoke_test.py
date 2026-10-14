"""Offline smoke test — exercises the DB layer with fake API payloads."""
import json
import sys
import tempfile
from pathlib import Path

import api
import collect
import db

FOREVER = api.FOREVER_TYPE_ID
RETAIL = 517


def mod(mid, name, created, files):
    return {
        "id": mid, "slug": name.lower().replace(" ", "-"), "name": name,
        "summary": f"{name} summary", "classId": 1, "primaryCategoryId": 1009,
        "categories": [{"id": 1009}, {"id": 1017}],
        "authors": [{"id": 900 + mid, "name": f"author{mid}"}],
        "links": {"websiteUrl": "https://w", "sourceUrl": "https://gh", "issuesUrl": None},
        "dateCreated": created, "dateReleased": created, "dateModified": created,
        "allowModDistribution": True, "downloadCount": 1000 * mid,
        "thumbsUpCount": mid, "rating": None, "gamePopularityRank": mid,
        "latestFiles": files,
    }


def f(fid, types, dl):
    return {
        "id": fid, "displayName": f"v{fid}", "fileName": f"f{fid}.zip",
        "fileDate": "2026-10-01T00:00:00Z", "fileLength": 123, "releaseType": 1,
        "downloadCount": dl, "gameVersions": ["1.0.0"],
        "sortableGameVersions": [{"gameVersionTypeId": t} for t in types],
    }


tmp = Path(tempfile.mkdtemp()) / "test.db"
conn = db.connect(tmp)
print(f"schema ok -> {tmp}")

db.save_categories(conn, [
    {"id": 1, "name": "Addons", "slug": "addons", "classId": None, "isClass": True},
    {"id": 1009, "name": "Bags & Inventory", "slug": "bags", "classId": 1, "isClass": False},
])

# --- run 1 -------------------------------------------------------------
run1 = db.start_run(conn, "daily")
t1 = db.now_iso()

cases = [
    # Forever-only file -> native
    (mod(1, "Native Addon", "2026-09-20T00:00:00Z", [f(11, [FOREVER], 100)]), "native"),
    # multi-flavor only, old project -> crossflavor
    (mod(2, "Veteran Addon", "2015-01-01T00:00:00Z", [f(22, [FOREVER, RETAIL], 5000)]), "crossflavor"),
    # multi-flavor but new project -> native by era
    (mod(3, "New Multi", "2026-09-25T00:00:00Z", [f(33, [FOREVER, RETAIL], 7)]), "native"),
    # no forever file visible in latestFiles -> unknown
    (mod(4, "Mystery", "2014-01-01T00:00:00Z", [f(44, [RETAIL], 9)]), "unknown"),
]

for m, expected in cases:
    tier = collect.process_mod(conn, run1, None, m, t1, full_files=False)
    assert tier == expected, f"{m['name']}: got {tier!r}, expected {expected!r}"
    print(f"  tier ok: {m['name']:<16} -> {tier}")
conn.commit()
db.finish_run(conn, run1, "ok", 3, len(cases))

# attribution check
row = conn.execute(
    "SELECT * FROM snapshots WHERE run_id=? AND addon_id=2", (run1,)
).fetchone()
assert row["forever_any_downloads"] == 5000, row["forever_any_downloads"]
assert row["forever_only_downloads"] == 0, row["forever_only_downloads"]
print("  multi-flavor downloads excluded from attributable total ✓")

row = conn.execute(
    "SELECT * FROM snapshots WHERE run_id=? AND addon_id=1", (run1,)
).fetchone()
assert row["forever_only_downloads"] == 100
print("  forever-only downloads attributed ✓")

# --- run 2, with movement ---------------------------------------------
run2 = db.start_run(conn, "daily")
t2 = db.now_iso()
moved = [
    mod(1, "Native Addon", "2026-09-20T00:00:00Z", [f(11, [FOREVER], 180)]),
    mod(2, "Veteran Addon", "2015-01-01T00:00:00Z", [f(22, [FOREVER, RETAIL], 5000)]),
    mod(3, "New Multi", "2026-09-25T00:00:00Z", [f(33, [FOREVER, RETAIL], 7)]),
    mod(4, "Mystery", "2014-01-01T00:00:00Z", [f(44, [RETAIL], 9)]),
]
moved[0]["downloadCount"] = 1500          # +500
for m in moved:
    collect.process_mod(conn, run2, None, m, t2, full_files=False)
conn.commit()
db.finish_run(conn, run2, "ok", 3, len(moved))

collect.report(conn, run2)

# --- integrity ---------------------------------------------------------
counts = {
    t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    for t in ("runs", "addons", "files", "snapshots", "file_snapshots",
              "addon_categories", "categories")
}
print("\nrow counts:", counts)
assert counts["addons"] == 4 and counts["snapshots"] == 8 and counts["file_snapshots"] == 8
assert counts["files"] == 4, "files should be upserted, not duplicated"

first_seen = conn.execute("SELECT first_seen_run FROM addons WHERE id=1").fetchone()[0]
assert first_seen == run1, f"first_seen_run was overwritten: {first_seen}"
last_seen = conn.execute("SELECT last_seen_run FROM addons WHERE id=1").fetchone()[0]
assert last_seen == run2
print("first_seen_run preserved, last_seen_run updated ✓")

types = json.loads(conn.execute("SELECT version_type_ids FROM files WHERE id=22").fetchone()[0])
assert types == sorted([RETAIL, FOREVER]), types
assert conn.execute("SELECT is_multi_flavor FROM files WHERE id=22").fetchone()[0] == 1
print("multi-flavor flag + version types stored ✓")

print("\nALL CHECKS PASSED")
