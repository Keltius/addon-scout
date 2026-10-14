"""
Follow-up spike — do file downloadCounts and project downloadCount agree?

The first spike found a single-file addon where the project reported 182
downloads and its only file reported 2. For a one-file addon those should
match. This checks whether that gap is systematic (different measurement)
or noise (reporting lag on a brand-new addon).

Tests established addons with real numbers, where the signal is clearest.

Usage:  python verify_downloads.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.curseforge.com/v1"
RAW = Path("raw")
GAME_ID = 1
FOREVER_ID = 88568
SAMPLE = 12

API_KEY = os.environ.get("CURSEFORGE_API_KEY")
if not API_KEY:
    sys.exit("CURSEFORGE_API_KEY is not set.")


def get(path, **params):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": API_KEY,
            "Accept": "application/json",
            "User-Agent": "addon-scout/0.1 (personal analytics spike)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {path}\n{e.read().decode('utf-8', 'replace')[:300]}")
    time.sleep(0.4)
    return payload


def all_files(mod_id, max_files=400, **extra):
    """Page through a mod's files (pageSize caps at 50)."""
    out, index = [], 0
    while index < max_files:
        batch = get(f"/mods/{mod_id}/files", index=index, pageSize=50, **extra).get("data", [])
        out.extend(batch)
        if len(batch) < 50:
            break
        index += 50
    return out


print("Fetching the most-downloaded Forever addons...\n")

search = get(
    "/mods/search",
    gameId=GAME_ID,
    gameVersionTypeId=FOREVER_ID,
    sortField=6,          # TotalDownloads
    sortOrder="desc",
    pageSize=SAMPLE,
)
mods = search.get("data", [])
RAW.mkdir(exist_ok=True)
(RAW / "verify_search.json").write_text(json.dumps(search, indent=2), encoding="utf-8")

rows = []
print(f"{'addon':<32} {'project':>11} {'all files':>11} {'ratio':>7} "
      f"{'files':>6} {'fvr':>5} {'multi':>6}")
print("-" * 84)

for m in mods:
    mod_id = m["id"]
    project_dl = m.get("downloadCount") or 0

    files = all_files(mod_id)
    fsum = sum(f.get("downloadCount") or 0 for f in files)

    fvr, multi = 0, 0
    for f in files:
        types = {s.get("gameVersionTypeId") for s in f.get("sortableGameVersions", [])}
        if FOREVER_ID in types:
            fvr += 1
            if types - {FOREVER_ID}:
                multi += 1

    ratio = (fsum / project_dl) if project_dl else 0
    rows.append((m.get("name"), project_dl, fsum, ratio, len(files), fvr, multi))

    print(f"{str(m.get('name'))[:31]:<32} {project_dl:>11,} {fsum:>11,} "
          f"{ratio:>6.1%} {len(files):>6} {fvr:>5} {multi:>6}")

(RAW / "verify_rows.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

# ------------------------------------------------------------------ verdict
print("\n" + "=" * 84)

ratios = [r[3] for r in rows if r[1] > 0]
if ratios:
    lo, hi = min(ratios), max(ratios)
    avg = sum(ratios) / len(ratios)
    print(f"file-sum / project-total:  low {lo:.1%}   avg {avg:.1%}   high {hi:.1%}")

    if lo > 0.9 and hi < 1.1:
        print("\n  => They AGREE. The 182-vs-2 case was reporting lag on a new addon.")
        print("     Forever share = sum(forever files) / project total is valid.")
    elif hi - lo < 0.15:
        print(f"\n  => CONSISTENT OFFSET (~{avg:.0%}). They measure different things,")
        print("     but the ratio is stable, so file counts are still usable for")
        print("     comparing addons to each other.")
    else:
        print("\n  => INCONSISTENT. The ratio varies a lot between addons, so")
        print("     file sums and project totals cannot be mixed in one formula.")
        print("     Pick one and use it alone. Store both; decide later.")

total_fvr = sum(r[5] for r in rows)
total_multi = sum(r[6] for r in rows)
print(f"\nForever-tagged files: {total_fvr}")
print(f"  ...also tagged for another flavor: {total_multi} "
      f"({(total_multi / total_fvr) if total_fvr else 0:.0%})")
print("  (High = many files can't be attributed to Forever alone.)")
