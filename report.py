"""
Build a shareable HTML report from the collected snapshots and open it.

Designed to be screenshotted into Discord: fixed dark theme, one idea per
card, large readable numbers, nothing that needs scrolling to make sense.

    python report.py                # build + open in browser
    python report.py --no-open      # just write report.html
    python report.py --days 7       # compare against ~7 days ago
"""

import argparse
import html
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).with_name("addon_scout.db")
OUT = Path(__file__).with_name("report.html")
CF_URL = "https://www.curseforge.com/wow/addons/"

# Validated for the dark surface #1a1a19 (dataviz slots 1 and 2).
ACCENT = "#3987e5"
ACCENT_2 = "#d95926"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def esc(s):
    return html.escape(str(s) if s is not None else "")


def ago(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = (datetime.now(timezone.utc) - d).days
    if days < 1:
        return "today"
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        return f"{days // 30} mo ago"
    return f"{days // 365}y ago"


# --------------------------------------------------------------- components
def bar_rows(rows, value_key, label_fmt, accent=ACCENT,
             empty="Nothing to show yet — needs another collector run."):
    """A ranked bar list: name, thin proportional bar, direct value label."""
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    top = max((r[value_key] or 0) for r in rows) or 1
    out = []
    for r in rows:
        v = r[value_key] or 0
        pct = max(1.5, 100 * v / top)
        keys = r.keys()
        slug = r["slug"] if "slug" in keys else None
        name = esc(r["name"])
        link = f'<a href="{CF_URL}{esc(slug)}">{name}</a>' if slug else name

        logo = r["logo_url"] if "logo_url" in keys else None
        art = (f'<img class="logo" src="{esc(logo)}" alt="" loading="lazy"'
               f' onerror="this.style.visibility=\'hidden\'">' if logo
               else '<div class="logo"></div>')

        summary = r["summary"] if "summary" in keys else None
        if summary and len(summary) > 115:
            summary = summary[:112].rstrip() + "…"
        desc = f'<div class="desc">{esc(summary)}</div>' if summary else ""

        meta = []
        if "author_name" in keys and r["author_name"]:
            meta.append(esc(r["author_name"]))
        if "date_created" in keys and r["date_created"]:
            meta.append(ago(r["date_created"]))
        if "date_modified" in keys and r["date_modified"]:
            meta.append(f"updated {ago(r['date_modified'])}")

        out.append(f"""
        <li>
          {art}
          <div class="body">
            <div class="row-head">
              <span class="nm">{link}</span>
              <span class="val">{label_fmt(v)}</span>
            </div>
            {desc}
            <div class="track"><div class="fill" style="width:{pct:.1f}%;background:{accent}"></div></div>
            <div class="meta">{esc(' · '.join(meta))}</div>
          </div>
        </li>""")
    return f'<ol class="bars">{"".join(out)}</ol>'


def family_map(conn):
    """addon_id -> family root, via declared RequiredDependency (type 3).

    Resolves one hop up, then collapses chains, so a module of a module lands
    on the same root. Only edges pointing at an addon we actually track count;
    a dependency on some Retail-only library is not a family.
    """
    parent = {}
    for r in q(conn, """SELECT d.addon_id, d.depends_on_id FROM dependencies d
                        JOIN addons a ON a.id = d.depends_on_id
                        WHERE d.relation_type = 3"""):
        parent.setdefault(r["addon_id"], r["depends_on_id"])

    def root(aid, depth=0):
        p = parent.get(aid)
        if p is None or p == aid or depth > 4:
            return aid
        return root(p, depth + 1)

    return {aid: root(aid) for aid in parent}


def pick(rows, seen, fams, limit=10, per_author=1, per_family=1):
    """Thin a ranked list before display.

    Three separate problems, all of which showed up in real reports:
      - one author taking 3 of 10 slots (rustykey's language packs)
      - a family taking several slots (DBM-Core beside DBM-Dungeons)
      - the same addon appearing in three different cards
    `seen` is shared across cards, so earlier cards win.
    """
    out, authors, families = [], {}, {}
    for r in rows:
        aid = r["addon_id"]
        if aid in seen:
            continue
        au = r["author_name"] or f"?{aid}"
        fam = fams.get(aid, aid)
        if authors.get(au, 0) >= per_author or families.get(fam, 0) >= per_family:
            continue
        authors[au] = authors.get(au, 0) + 1
        families[fam] = families.get(fam, 0) + 1
        out.append(r)
        seen.add(aid)
        if len(out) >= limit:
            break
    return out


def card(title, subtitle, body):
    return f"""
    <section class="card">
      <h2>{esc(title)}</h2>
      <p class="sub">{esc(subtitle)}</p>
      {body}
    </section>"""


# -------------------------------------------------------------------- build
def build(conn, days):
    runs = q(conn, "SELECT * FROM runs WHERE status='ok' ORDER BY id DESC")
    if not runs:
        sys.exit("No completed runs. Run collect.py first.")
    cur = runs[0]
    run_id = cur["id"]

    # Comparison run: the newest good run at least `days` old, else the previous
    # one. Only full runs count — comparing against a --limit test run produces
    # nonsense like "+5,062 new addons".
    full = [r for r in runs[1:] if r["addons_seen"] >= 0.9 * cur["addons_seen"]]
    prev = None
    # Fractional days with slack: a scheduled run that fires 23h58m after the
    # last one is still "yesterday". Integer .days silently skipped a day and
    # produced a 2-day window labelled as one.
    want = max(days - 0.25, 0)
    for r in full:
        gap = (datetime.fromisoformat(cur["started_at"])
               - datetime.fromisoformat(r["started_at"])).total_seconds() / 86400
        if gap >= want:
            prev = r
            break
    if prev is None and full:
        prev = full[0]

    tiers = {r["tier"]: r["n"] for r in q(
        conn, "SELECT tier, COUNT(*) n FROM snapshots WHERE run_id=? GROUP BY tier",
        (run_id,))}
    total = sum(tiers.values())

    new_count = q(conn, "SELECT COUNT(*) n FROM addons WHERE first_seen_run=?",
                  (run_id,))[0]["n"] if prev else 0

    cards = []
    fams = family_map(conn)
    seen = set()

    # --- Rising: biggest download gains -----------------------------------
    if prev:
        secs = (datetime.fromisoformat(cur["started_at"])
                - datetime.fromisoformat(prev["started_at"])).total_seconds()
        span = (f"{secs / 86400:.1f} days" if secs >= 86400
                else f"{secs / 3600:.1f} hours")
        rising = q(conn, """
            SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
                   ad.logo_url, a.tier,
                   a.project_downloads - b.project_downloads AS gain
            FROM snapshots a
            JOIN snapshots b ON b.addon_id = a.addon_id AND b.run_id = ?
            JOIN addons ad ON ad.id = a.addon_id
            WHERE a.run_id = ? AND gain > 0
            ORDER BY gain DESC LIMIT 80""", (prev["id"], run_id))
        cards.append(card(
            "🔥 Most downloaded today",
            f"Raw downloads gained over the last {span} — the giants win this one. One per author, one per addon family.",
            bar_rows(pick(rising, seen, fams), "gain", lambda v: f"+{v:,}")))

        # Absolute gain just re-ranks by size: Details! gaining 345k on a base
        # of 370M is 0.09% growth. Rate is the momentum question. The floor
        # keeps 2 -> 20 downloads off the leaderboard.
        FLOOR = 500
        fast = q(conn, """
            SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
                   ad.logo_url, ad.date_created, a.tier,
                   1.0 * (a.project_downloads - b.project_downloads)
                       / b.project_downloads AS rate
            FROM snapshots a
            JOIN snapshots b ON b.addon_id = a.addon_id AND b.run_id = ?
            JOIN addons ad ON ad.id = a.addon_id
            WHERE a.run_id = ? AND b.project_downloads >= ?
              AND a.project_downloads > b.project_downloads
            ORDER BY rate DESC LIMIT 80""", (prev["id"], run_id, FLOOR))
        cards.append(card(
            "🚀 Fastest growing",
            f"% growth over the same window ({FLOOR}+ downloads already, so "
            "tiny numbers can't game it)",
            bar_rows(pick(fast, seen, fams), "rate", lambda v: f"+{v:.0%}" if v >= 0.1
                     else f"+{v*100:.1f}%")))

        gems = q(conn, """
            SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
                   ad.logo_url, ad.date_created, a.tier,
                   a.project_downloads - b.project_downloads AS gain
            FROM snapshots a
            JOIN snapshots b ON b.addon_id = a.addon_id AND b.run_id = ?
            JOIN addons ad ON ad.id = a.addon_id
            WHERE a.run_id = ? AND a.tier = 'native'
              AND a.project_downloads < 20000 AND gain > 0
            ORDER BY gain DESC LIMIT 80""", (prev["id"], run_id))
        cards.append(card(
            "💎 Hidden gems",
            "Under 20k lifetime downloads, but growing fastest right now",
            bar_rows(pick(gems, seen, fams), "gain", lambda v: f"+{v:,}", ACCENT_2)))
    else:
        cards.append(card(
            "📈 Biggest movers",
            "Needs two collector runs — growth can't be measured from one snapshot",
            '<p class="empty">Run collect.py again tomorrow and this fills in.</p>'))

    # --- Families: the payoff for storing dependencies --------------------
    if prev:
        gains = q(conn, """
            SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name,
                   ad.logo_url,
                   a.project_downloads - b.project_downloads AS gain
            FROM snapshots a
            JOIN snapshots b ON b.addon_id = a.addon_id AND b.run_id = ?
            JOIN addons ad ON ad.id = a.addon_id
            WHERE a.run_id = ? AND a.project_downloads > b.project_downloads""",
            (prev["id"], run_id))

        roots = {}
        for r in gains:
            root = fams.get(r["addon_id"], r["addon_id"])
            f = roots.setdefault(root, {"gain": 0, "n": 0, "head": None,
                                        "authors": []})
            f["gain"] += r["gain"] or 0
            f["n"] += 1
            f["authors"].append(r["author_name"])
            if r["addon_id"] == root:
                f["head"] = r

        fam_rows = []
        for root, f in roots.items():
            # A "family" needs the parent plus at least two satellites, and we
            # need the parent's own row for a name and logo.
            if f["n"] < 3 or not f["head"]:
                continue
            h = f["head"]
            # Only sum a SUITE — one author's own modules. A shared library
            # (Ace3: 29 dependents, 0 by its author) or a plugin ecosystem
            # (Questie: 17 dependents, 15 different authors) is not one
            # product, and adding its members up would be meaningless.
            same = sum(1 for a in f["authors"] if a and a == h["author_name"])
            if same * 2 < f["n"]:
                continue
            fam_rows.append({
                "addon_id": root, "name": h["name"], "slug": h["slug"],
                "author_name": h["author_name"], "logo_url": h["logo_url"],
                "summary": f"{f['n']} addons in this family moved together",
                "gain": f["gain"],
            })
        fam_rows.sort(key=lambda r: -r["gain"])

        cards.append(card(
            "\U0001F3DB\uFE0F Hottest families",
            "One author's module suite, summed. A base's own growth "
            "understates it — most module installs come from people who "
            "already had the base. Shared libraries are excluded.",
            bar_rows(fam_rows[:8], "gain", lambda v: f"+{v:,}", ACCENT_2,
                     empty="No families with 3+ moving members yet.")))

    # --- New arrivals (works from a single snapshot) -----------------------
    newest = q(conn, """
        SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
               ad.logo_url, ad.date_created, s.project_downloads AS dl
        FROM snapshots s JOIN addons ad ON ad.id = s.addon_id
        WHERE s.run_id = ? AND ad.date_created >= date('now', '-14 days')
        ORDER BY dl DESC LIMIT 600""", (run_id,))
    cards.append(card(
        "✨ New arrivals",
        "Projects created in the last 14 days, by downloads so far",
        bar_rows(pick(newest, seen, fams), "dl", lambda v: f"{v:,}")))

    # --- Veterans: stale check -------------------------------------------
    # The download floor matters: without it this fills with tiny addons from
    # one prolific author and calls them "big names".
    FLOOR = 100_000
    stale = q(conn, """
        SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
               ad.logo_url, s.date_modified, s.project_downloads AS dl
        FROM snapshots s JOIN addons ad ON ad.id = s.addon_id
        WHERE s.run_id = ? AND s.tier = 'crossflavor'
          AND s.project_downloads >= ?
          AND s.date_modified < date('now', '-30 days')
        ORDER BY dl DESC LIMIT 60""", (run_id, FLOOR))
    cards.append(card(
        "🕰️ Big names going stale",
        f"{FLOOR//1000}k+ downloads, supports Forever, but no update in 30+ days",
        bar_rows(pick(stale, seen, fams, limit=8), "dl", lambda v: f"{v:,}", ACCENT_2,
                 empty="None — every big cross-flavor addon has been updated "
                       "in the last 30 days.")))

    gen = datetime.now().strftime("%d %b %Y, %H:%M")
    stat_row = f"""
      <div class="stats">
        <div><b>{total:,}</b><span>Forever addons</span></div>
        <div><b>{tiers.get('native', 0):,}</b><span>Forever-native</span></div>
        <div><b>{tiers.get('crossflavor', 0):,}</b><span>cross-flavor</span></div>
        {f'<div><b>+{new_count:,}</b><span>new since last run</span></div>' if prev else ''}
      </div>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WoW Forever Addon Report</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 16px 56px;
    background: #121211; color: #f0efec;
    font: 15px/1.5 "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  header {{ margin-bottom: 24px; }}
  h1 {{ font-size: 25px; margin: 0 0 4px; letter-spacing: -.02em; }}
  .when {{ color: #8f8e86; font-size: 13px; margin: 0; }}
  .stats {{
    display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 28px;
  }}
  .stats div {{
    flex: 1 1 120px; background: #1a1a19; border: 1px solid #2c2c2a;
    border-radius: 10px; padding: 12px 14px;
  }}
  .stats b {{
    display: block; font-size: 23px; font-variant-numeric: tabular-nums;
    letter-spacing: -.02em;
  }}
  .stats span {{ color: #8f8e86; font-size: 12px; }}
  .card {{
    background: #1a1a19; border: 1px solid #2c2c2a; border-radius: 12px;
    padding: 20px 22px 8px; margin-bottom: 18px;
  }}
  h2 {{ font-size: 17px; margin: 0 0 2px; }}
  .sub {{ color: #8f8e86; font-size: 13px; margin: 0 0 18px; }}
  ol.bars {{ list-style: none; margin: 0; padding: 0; counter-reset: r; }}
  ol.bars li {{ display: flex; gap: 12px; margin-bottom: 17px; counter-increment: r; }}
  .logo {{ width: 40px; height: 40px; border-radius: 9px; flex: none;
           object-fit: cover; background: #262624; margin-top: 2px; }}
  .body {{ flex: 1; min-width: 0; }}
  .desc {{ color: #a8a79e; font-size: 12.5px; margin: 1px 0 6px;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .row-head {{ display: flex; justify-content: space-between; gap: 12px;
               align-items: baseline; margin-bottom: 5px; }}
  .nm {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .nm::before {{ content: counter(r) ". "; color: #6a6a64; }}
  .nm a {{ color: #f0efec; text-decoration: none; }}
  .nm a:hover {{ text-decoration: underline; }}
  .val {{ font-variant-numeric: tabular-nums; color: #c3c2b7;
          font-size: 14px; flex: none; }}
  .track {{ height: 7px; background: #262624; border-radius: 4px; overflow: hidden; }}
  .fill {{ height: 100%; border-radius: 4px; }}
  .meta {{ color: #77766f; font-size: 12px; margin-top: 4px; }}
  .empty {{ color: #8f8e86; font-size: 14px; padding: 4px 0 16px; }}
  footer {{ color: #6a6a64; font-size: 12px; text-align: center; margin-top: 26px; }}
</style></head>
<body><div class="wrap">
  <header>
    <h1>WoW Forever — Addon Report</h1>
    <p class="when">{esc(gen)} · ranked by growth, not lifetime downloads</p>
  </header>
  {stat_row}
  {''.join(cards)}
  <footer>Addon Scout · data from the CurseForge API</footer>
</div></body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB))
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--days", type=int, default=1,
                   help="compare against a run at least this many days older")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    path = Path(args.db)
    if not path.exists():
        sys.exit(f"No database at {path.resolve()} — run collect.py first.")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = Path(args.out)
    out.write_text(build(conn, args.days), encoding="utf-8")
    conn.close()

    print(f"Wrote {out.resolve()}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
