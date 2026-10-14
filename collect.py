"""
Addon Scout collector — one snapshot of every Forever addon, into SQLite.

Why this runs before the scoring exists: CurseForge serves no history. Growth
rate can only be computed from snapshots we take ourselves, so every day
without a run is a day of data that cannot be recovered later.

    python collect.py                 # daily run (~105 API calls, ~2 min)
    python collect.py --limit 200     # quick test
    python collect.py --full-files    # weekly: complete Forever file lists
    python collect.py --resume        # continue an interrupted run
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import api
import db

# A project created after this is Forever-era, whatever its files are tagged.
# Forever beta opened 2026-09-17; this is a deliberately loose cutoff.
FOREVER_ERA_START = "2026-06-01"

log = logging.getLogger("collect")


def setup_logging(verbose):
    logs = Path(__file__).with_name("logs")
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logs / f"collect-{stamp}.log", encoding="utf-8"),
        ],
    )


def classify(mod, forever_files, forever_only_files):
    """native  = downloads are attributable to Forever
       crossflavor = one package serves several flavors; attribution impossible

    Raw inputs are stored alongside this, so the rule can be rewritten later
    without recollecting.
    """
    if forever_only_files:
        return "native"
    created = (mod.get("dateCreated") or "")[:10]
    if created and created >= FOREVER_ERA_START:
        return "native"
    if forever_files:
        return "crossflavor"
    return "unknown"


def process_mod(conn, run_id, cf, mod, taken_at, full_files):
    """Store one addon plus whatever file data we have for it."""
    db.save_addon(conn, run_id, mod)
    addon_id = mod["id"]

    if full_files:
        files = cf.mod_files(addon_id)
    else:
        # The search response already embeds latestFiles as full File objects.
        # Good enough for tier + velocity; no extra call.
        files = mod.get("latestFiles") or []

    n_forever = n_only = 0
    dl_any = dl_only = 0
    for f in files:
        if not f.get("id"):
            continue
        is_forever, is_multi = db.save_file(
            conn, run_id, addon_id, f, api.FOREVER_TYPE_ID, taken_at
        )
        if is_forever:
            n_forever += 1
            dl_any += f.get("downloadCount") or 0
            if not is_multi:
                n_only += 1
                dl_only += f.get("downloadCount") or 0

    stats = {
        "tier": classify(mod, n_forever, n_only),
        "forever_files": n_forever,
        "forever_only_files": n_only,
        "forever_any_downloads": dl_any,
        "forever_only_downloads": dl_only,
    }
    db.save_snapshot(conn, run_id, addon_id, taken_at, mod, stats)
    return stats["tier"]


def report(conn, run_id):
    """What changed since the last good run. On day 2 this is the first real
    test of whether download counts move at all."""
    prev = db.previous_run(conn, run_id)
    cur = conn.execute(
        """SELECT tier, COUNT(*) n FROM snapshots WHERE run_id = ? GROUP BY tier""",
        (run_id,),
    ).fetchall()

    print("\n" + "=" * 62)
    print("Run summary")
    print("=" * 62)
    for row in cur:
        print(f"  {row['tier']:<12} {row['n']:>6} addons")

    if not prev:
        print("\n  First run — no comparison yet. Run again tomorrow.")
        return

    print(f"\n  Comparing against run {prev['id']} ({prev['started_at'][:16]})")

    moved = conn.execute(
        """SELECT COUNT(*) n FROM snapshots a
           JOIN snapshots b ON a.addon_id = b.addon_id AND b.run_id = ?
           WHERE a.run_id = ? AND a.project_downloads <> b.project_downloads""",
        (prev["id"], run_id),
    ).fetchone()["n"]

    fmoved = conn.execute(
        """SELECT COUNT(*) n FROM file_snapshots a
           JOIN file_snapshots b ON a.file_id = b.file_id AND b.run_id = ?
           WHERE a.run_id = ? AND a.downloads <> b.downloads""",
        (prev["id"], run_id),
    ).fetchone()["n"]

    print(f"  Addons whose project total changed: {moved}")
    print(f"  Files whose download count changed: {fmoved}")
    if fmoved == 0:
        print("\n  ⚠ No file counts moved. If this repeats, per-file numbers are")
        print("    stale and velocity must come from project totals instead.")

    new = conn.execute(
        "SELECT COUNT(*) n FROM addons WHERE first_seen_run = ?", (run_id,)
    ).fetchone()["n"]
    print(f"  Addons seen for the first time: {new}")

    print("\n  Biggest movers (project downloads):")
    rows = conn.execute(
        """SELECT ad.name, ad.slug, a.project_downloads - b.project_downloads AS delta
           FROM snapshots a
           JOIN snapshots b ON a.addon_id = b.addon_id AND b.run_id = ?
           JOIN addons ad ON ad.id = a.addon_id
           WHERE a.run_id = ? AND delta > 0
           ORDER BY delta DESC LIMIT 10""",
        (prev["id"], run_id),
    ).fetchall()
    for r in rows:
        print(f"    {str(r['name'])[:40]:<42} +{r['delta']:,}")


def main():
    p = argparse.ArgumentParser(description="Collect a snapshot of Forever addons.")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    p.add_argument("--limit", type=int, help="stop after N addons (testing)")
    p.add_argument("--full-files", action="store_true",
                   help="fetch complete Forever file lists (slow; weekly)")
    p.add_argument("--resume", action="store_true",
                   help="continue the last interrupted run")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)
    conn = db.connect(Path(args.db))
    cf = api.CurseForge()

    start_index = 0
    run_id = None
    if args.resume:
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'interrupted' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            run_id, start_index = row["id"], row["last_index"]
            log.info("Resuming run %s from index %s", run_id, start_index)
        else:
            log.info("Nothing to resume; starting fresh.")

    if run_id is None:
        run_id = db.start_run(conn, mode="full-files" if args.full_files else "daily")

    taken_at = db.now_iso()
    seen = 0
    index = start_index
    status = "ok"

    try:
        log.info("Refreshing categories...")
        db.save_categories(conn, cf.categories())

        total = None
        while True:
            page = cf.search_page(index)
            mods = page.get("data", [])
            total = page.get("pagination", {}).get("totalCount", total)
            if not mods:
                break

            for mod in mods:
                process_mod(conn, run_id, cf, mod, taken_at, args.full_files)
                seen += 1

            conn.commit()
            index += len(mods)
            db.update_run(conn, run_id, last_index=index, addons_seen=seen)
            log.info("  %s / %s addons  (%s API calls)", seen, total, cf.calls)

            if args.limit and seen >= args.limit:
                log.info("Stopping at --limit %s", args.limit)
                break
            if total and index >= total:
                break

    except KeyboardInterrupt:
        status = "interrupted"
        log.warning("Interrupted. Resume with:  python collect.py --resume")
    except Exception as e:
        status = "failed"
        log.exception("Run failed: %s", e)
    finally:
        conn.commit()
        db.finish_run(conn, run_id, status, cf.calls, seen)
        log.info("Run %s %s — %s addons, %s API calls", run_id, status, seen, cf.calls)

    if status == "ok":
        report(conn, run_id)
    conn.close()
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
