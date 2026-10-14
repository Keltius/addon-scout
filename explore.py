"""
Phase 1 spike — confirm the assumptions Addon Scout is built on.

The docs tell us the File object HAS a downloadCount and a per-file
gameVersionTypeId. This script checks whether those fields are actually
populated for WoW Forever, and how many Forever addons exist.

Throwaway. Writes raw JSON to raw/ so we can design the schema against
real responses. Deliberately stdlib-only: runs with no venv, no pip.

Usage:
    setx CURSEFORGE_API_KEY "your-key"     (then open a NEW terminal)
    python explore.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.curseforge.com/v1"
RAW = Path("raw")

# The CurseForge website's Forever filter uses this. Verified in step 2.
FOREVER_TYPE_ID_GUESS = 88568

API_KEY = os.environ.get("CURSEFORGE_API_KEY")
if not API_KEY:
    sys.exit(
        "CURSEFORGE_API_KEY is not set.\n\n"
        "  Windows (this terminal only):  set CURSEFORGE_API_KEY=your-key\n"
        "  Windows (permanent):           setx CURSEFORGE_API_KEY \"your-key\"\n"
        "                                 then open a NEW terminal\n"
    )


def get(path, **params):
    """GET an endpoint, save the raw response, return parsed JSON."""
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
        body = e.read().decode("utf-8", "replace")[:400]
        hint = ""
        if e.code == 403:
            hint = "\n  403 usually means the key isn't reaching the API."
        elif e.code == 429:
            hint = "\n  429 = rate limited. Undocumented, so now we know."
        sys.exit(f"\nHTTP {e.code} on {path}\n{body}{hint}\n")
    except urllib.error.URLError as e:
        sys.exit(f"\nCould not reach the API: {e.reason}\n")

    RAW.mkdir(exist_ok=True)
    name = (path.strip("/").replace("/", "_") + "_" + "_".join(
        f"{k}-{v}" for k, v in params.items()))[:120]
    (RAW / f"{name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    time.sleep(0.4)  # be polite; no documented rate limit
    return payload


def rule(title):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def days_since(iso):
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


findings = {}

# ---------------------------------------------------------------- 1. game id
rule("1. World of Warcraft's game ID")

games = get("/games", pageSize=50).get("data", [])
wow = next((g for g in games if "warcraft" in g.get("name", "").lower()), None)
if not wow:
    print("WoW not found. Games available:")
    for g in games:
        print(f"  {g.get('id'):>6}  {g.get('name')}")
    sys.exit(1)

GAME_ID = wow["id"]
findings["game_id"] = GAME_ID
print(f"  gameId = {GAME_ID}   ({wow.get('name')}, slug: {wow.get('slug')})")


# ------------------------------------------------------- 2. version type ids
rule("2. Version types — which one is Forever?")

vtypes = get(f"/games/{GAME_ID}/version-types").get("data", [])
by_id = {v.get("id"): v.get("name") for v in vtypes}

for vt in sorted(vtypes, key=lambda v: v.get("id", 0)):
    flag = ""
    if vt.get("id") == FOREVER_TYPE_ID_GUESS:
        flag = "   <== the website's Forever filter"
    elif "forever" in str(vt.get("name", "")).lower():
        flag = "   <== name says Forever"
    print(f"  {vt.get('id'):>8}  {vt.get('name')}{flag}")

named = [v for v in vtypes if "forever" in str(v.get("name", "")).lower()]
if FOREVER_TYPE_ID_GUESS in by_id:
    FOREVER_ID = FOREVER_TYPE_ID_GUESS
    print(f"\n  CONFIRMED: {FOREVER_ID} exists, named {by_id[FOREVER_ID]!r}")
elif named:
    FOREVER_ID = named[0]["id"]
    print(f"\n  DIFFERENT FROM EXPECTED: using {FOREVER_ID} ({named[0]['name']!r})")
else:
    print(f"\n  {FOREVER_TYPE_ID_GUESS} not in the list and no type named Forever.")
    sys.exit("  Stopping — pick the right id from the list above and re-run.")

findings["forever_version_type_id"] = FOREVER_ID


# ----------------------------------------------- 3. how many Forever addons?
rule("3. How many addons support Forever?")

search = get(
    "/mods/search",
    gameId=GAME_ID,
    gameVersionTypeId=FOREVER_ID,
    sortField=11,          # ReleasedDate
    sortOrder="desc",
    pageSize=50,
)
page = search.get("pagination", {})
mods = search.get("data", [])
total = page.get("totalCount")
findings["forever_addon_count"] = total

print(f"  totalCount = {total}   (addons with at least one Forever-tagged file)")
print(f"  pagination: {page}")
print(f"\n  Pages needed at 50/page: {(total + 49) // 50 if total else '?'}")
print("  (Hard cap is index + pageSize <= 10,000.)\n")

print("  10 most recently released:")
for m in mods[:10]:
    age = days_since(m.get("dateReleased"))
    print(f"    [{m.get('id'):>7}] {m.get('name')[:42]:<42} "
          f"{m.get('downloadCount'):>10,} dl  released {age}d ago")


# -------------------------------------------------------- 4. the real test
rule("4. THE KEY TEST — are per-file download counts populated?")

if not mods:
    sys.exit("  No mods returned; cannot continue.")

# Pick a mod with a decent download count so zeros are meaningful.
target = max(mods, key=lambda m: m.get("downloadCount", 0))
MOD_ID = target["id"]

print(f"  Testing: {target.get('name')}  (modId {MOD_ID})")
print(f"  Lifetime downloads, ALL flavors: {target.get('downloadCount'):,}\n")

all_files = get(f"/mods/{MOD_ID}/files", pageSize=50).get("data", [])
fv_files = get(
    f"/mods/{MOD_ID}/files", gameVersionTypeId=FOREVER_ID, pageSize=50
).get("data", [])

print(f"  Files total:          {len(all_files)}")
print(f"  Files tagged Forever: {len(fv_files)}   "
      f"(server-side filter — we don't have to sort them ourselves)\n")

forever_sum = sum(f.get("downloadCount") or 0 for f in fv_files)
zero = sum(1 for f in fv_files if not f.get("downloadCount"))

for f in fv_files[:8]:
    types = {s.get("gameVersionTypeId") for s in f.get("sortableGameVersions", [])}
    other = types - {FOREVER_ID}
    mixed = f"  MULTI-FLAVOR: also {sorted(other)}" if other else ""
    print(f"    {f.get('fileName')[:44]:<44} "
          f"{(f.get('downloadCount') or 0):>9,} dl   {f.get('fileDate', '')[:10]}{mixed}")

print(f"\n  Sum of Forever-tagged file downloads: {forever_sum:,}")
if target.get("downloadCount"):
    pct = 100 * forever_sum / target["downloadCount"]
    print(f"  That's {pct:.1f}% of the lifetime total.")
print(f"  Files reporting zero downloads: {zero} of {len(fv_files)}")

findings["per_file_downloads_work"] = forever_sum > 0

print()
if forever_sum > 0:
    print("  ✓ PER-FILE COUNTS WORK. The core metric is viable.")
else:
    print("  ✗ ALL ZERO. downloadCount exists but isn't populated per file.")
    print("    The Forever-specific metric needs a rethink — we'd have to")
    print("    derive adoption from daily deltas on the project total instead.")


# ------------------------------------------------------------ 5. categories
rule("5. Addon categories (for your interest ratings)")

cats = get("/categories", gameId=GAME_ID).get("data", [])
classes = [c for c in cats if c.get("isClass")]
addon_class = next((c for c in classes if "addon" in c.get("name", "").lower()), None)

if addon_class:
    findings["addon_class_id"] = addon_class["id"]
    subs = [c for c in cats if c.get("classId") == addon_class["id"]]
    print(f"  classId = {addon_class['id']}  ({len(subs)} categories)\n")
    for c in sorted(subs, key=lambda c: c.get("name", "")):
        print(f"    {c.get('id'):>6}  {c.get('name')}")
else:
    print("  No 'Addons' class. Classes found:")
    for c in classes:
        print(f"    {c.get('id'):>6}  {c.get('name')}")


# --------------------------------------------------------------- 6. summary
rule("Summary — paste this back into the chat")

for k, v in findings.items():
    print(f"  {k}: {v}")

print(f"\n  Raw JSON in {RAW.resolve()}  (gitignored)")
