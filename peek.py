"""
Look inside the database. Read-only.

    python peek.py                      # overview
    python peek.py --tier native        # top addons in one tier
    python peek.py --addon questie      # everything about one addon
    python peek.py --sql "SELECT ..."   # anything else
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).with_name("addon_scout.db")


def table(rows, headers=None):
    if not rows:
        print("  (nothing)")
        return
    headers = headers or rows[0].keys()
    data = [[("" if r[h] is None else str(r[h])) for h in headers] for r in rows]
    widths = [
        max(len(str(h)), max((len(d[i]) for d in data), default=0))
        for i, h in enumerate(headers)
    ]
    widths = [min(w, 44) for w in widths]
    print("  " + "  ".join(str(h)[:w].ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for d in data:
        print("  " + "  ".join(c[:w].ljust(w) for c, w in zip(d, widths)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB))
    p.add_argument("--addon", help="name or slug fragment")
    p.add_argument("--tier", choices=["native", "crossflavor", "unknown"])
    p.add_argument("--sql", help="run a query")
    p.add_argument("--limit", type=int, default=15)
    args = p.parse_args()

    path = Path(args.db)
    if not path.exists():
        sys.exit(f"No database at {path.resolve()} — run collect.py first.")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    size_mb = path.stat().st_size / 1_048_576
    print(f"\n{path.resolve()}  ({size_mb:.1f} MB)")

    # ------------------------------------------------------------ --sql
    if args.sql:
        print()
        table(conn.execute(args.sql).fetchall())
        return

    # ---------------------------------------------------------- --addon
    if args.addon:
        like = f"%{args.addon}%"
        row = conn.execute(
            """SELECT * FROM addons WHERE name LIKE ? OR slug LIKE ?
               ORDER BY LENGTH(name) LIMIT 1""", (like, like)
        ).fetchone()
        if not row:
            sys.exit(f"\nNo addon matching {args.addon!r}")

        print(f"\n{row['name']}  (id {row['id']}, slug {row['slug']})")
        print(f"  {row['summary']}")
        print(f"  author:   {row['author_name']}")
        print(f"  created:  {row['date_created']}")
        print(f"  modified: {row['date_modified']}")
        print(f"  source:   {row['source_url'] or '(none — no license info available)'}")

        print("\n  Categories:")
        table(conn.execute(
            """SELECT c.name FROM addon_categories ac JOIN categories c
               ON c.id = ac.category_id WHERE ac.addon_id = ?""", (row["id"],)
        ).fetchall())

        print("\n  Snapshots (the time series):")
        table(conn.execute(
            """SELECT taken_at, tier, project_downloads, forever_files,
                      forever_only_files, forever_only_downloads,
                      forever_any_downloads
               FROM snapshots WHERE addon_id = ? ORDER BY taken_at""", (row["id"],)
        ).fetchall())

        print("\n  Files:")
        table(conn.execute(
            """SELECT f.file_name, f.file_date, f.is_forever, f.is_multi_flavor,
                      fs.downloads
               FROM files f
               LEFT JOIN file_snapshots fs ON fs.file_id = f.id
               WHERE f.addon_id = ?
               GROUP BY f.id HAVING MAX(fs.taken_at)
               ORDER BY f.file_date DESC LIMIT 20""", (row["id"],)
        ).fetchall())
        return

    # --------------------------------------------------------- overview
    print("\nTables:")
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name")]
    for n in names:
        c = conn.execute(f"SELECT COUNT(*) c FROM {n}").fetchone()["c"]
        print(f"  {n:<20} {c:>8,} rows")

    print("\nRuns:")
    table(conn.execute(
        """SELECT id, started_at, status, mode, addons_seen, api_calls
           FROM runs ORDER BY id DESC LIMIT 10"""
    ).fetchall())

    last = conn.execute(
        "SELECT id FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not last:
        print("\nNo completed runs yet.")
        return
    run_id = last["id"]

    print(f"\nTiers (run {run_id}):")
    table(conn.execute(
        "SELECT tier, COUNT(*) addons FROM snapshots WHERE run_id=? GROUP BY tier",
        (run_id,)
    ).fetchall())

    where = "AND s.tier = ?" if args.tier else ""
    params = (run_id, args.tier) if args.tier else (run_id,)
    label = args.tier or "all tiers"

    print(f"\nTop by project downloads — {label} (lifetime, ALL flavors):")
    table(conn.execute(
        f"""SELECT a.name, s.tier, s.project_downloads, s.forever_files,
                   s.forever_only_files, s.forever_only_downloads
            FROM snapshots s JOIN addons a ON a.id = s.addon_id
            WHERE s.run_id = ? {where}
            ORDER BY s.project_downloads DESC LIMIT {args.limit}""", params
    ).fetchall())

    print(f"\nNewest projects — {label}:")
    table(conn.execute(
        f"""SELECT a.name, a.date_created, s.tier, s.project_downloads
            FROM snapshots s JOIN addons a ON a.id = s.addon_id
            WHERE s.run_id = ? {where}
            ORDER BY a.date_created DESC LIMIT {args.limit}""", params
    ).fetchall())

    multi = conn.execute(
        """SELECT SUM(is_multi_flavor) m, SUM(is_forever) f FROM files"""
    ).fetchone()
    if multi["f"]:
        print(f"\nForever files: {multi['f']:,} — of which multi-flavor "
              f"(not attributable): {multi['m']:,} ({multi['m']/multi['f']:.0%})")

    print("\nTip: python peek.py --addon <name>   for one addon's full history")


if __name__ == "__main__":
    main()
