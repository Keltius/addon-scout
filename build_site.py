"""
Build a small static site from the collected snapshots.

Three pages, three jobs:
  index.html    daily dashboard   — what moved since the last run (for you)
  weekly.html   the shareable one — compact, one screenshot, for Discord
  explore.html  every addon       — search, sort, filter, per-addon history

No server. Open site/index.html in a browser and click around. explore.html
reads site/data.json, so filtering and sorting happen in the browser with
nothing running.

    python build_site.py            # build + open index
    python build_site.py --no-open
"""

import argparse
import html
import json
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "addon_scout.db"
OUT = HERE / "site"
CF = "https://www.curseforge.com/wow/addons/"

ACCENT = "#3987e5"
ACCENT_2 = "#d95926"

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 16px 56px; background:#121211; color:#f0efec;
  font:15px/1.5 "Segoe UI", system-ui, sans-serif; -webkit-font-smoothing:antialiased; }
a { color:inherit; }
.wrap { max-width:860px; margin:0 auto; }
.wide { max-width:1180px; }
nav { display:flex; gap:4px; margin-bottom:22px; }
nav a { padding:7px 13px; border-radius:8px; font-size:13.5px; text-decoration:none;
  color:#a8a79e; border:1px solid transparent; }
nav a:hover { background:#1a1a19; color:#f0efec; }
nav a.on { background:#1a1a19; border-color:#2c2c2a; color:#f0efec; }
h1 { font-size:25px; margin:0 0 4px; letter-spacing:-.02em; }
.when { color:#8f8e86; font-size:13px; margin:0 0 20px; }
.stats { display:flex; flex-wrap:wrap; gap:10px; margin:0 0 24px; }
.stats div { flex:1 1 120px; background:#1a1a19; border:1px solid #2c2c2a;
  border-radius:10px; padding:12px 14px; }
.stats b { display:block; font-size:23px; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; }
.stats span { color:#8f8e86; font-size:12px; }
.card { background:#1a1a19; border:1px solid #2c2c2a; border-radius:12px;
  padding:20px 22px 8px; margin-bottom:18px; }
h2 { font-size:17px; margin:0 0 2px; }
.sub { color:#8f8e86; font-size:13px; margin:0 0 18px; }
ol.bars { list-style:none; margin:0; padding:0; counter-reset:r; }
ol.bars li { display:flex; gap:12px; margin-bottom:17px; counter-increment:r; }
.logo { width:40px; height:40px; border-radius:9px; flex:none; object-fit:cover;
  background:#262624; margin-top:2px; }
.bd { flex:1; min-width:0; }
.desc { color:#a8a79e; font-size:12.5px; margin:1px 0 6px; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.row-head { display:flex; justify-content:space-between; gap:12px;
  align-items:baseline; margin-bottom:5px; }
.nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.nm::before { content:counter(r) ". "; color:#6a6a64; }
.nm a { text-decoration:none; }
.nm a:hover { text-decoration:underline; }
.val { font-variant-numeric:tabular-nums; color:#c3c2b7; font-size:14px; flex:none; }
.track { height:7px; background:#262624; border-radius:4px; overflow:hidden; }
.fill { height:100%; border-radius:4px; }
.meta { color:#77766f; font-size:12px; margin-top:4px; }
.empty { color:#8f8e86; font-size:14px; padding:4px 0 16px; }
footer { color:#6a6a64; font-size:12px; text-align:center; margin-top:26px; }
"""


def esc(s):
    return html.escape("" if s is None else str(s))


def q(conn, sql, p=()):
    return conn.execute(sql, p).fetchall()


def ago(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    n = (datetime.now(timezone.utc) - d).days
    return ("today" if n < 1 else "1 day ago" if n == 1 else f"{n} days ago"
            if n < 30 else f"{n // 30} mo ago" if n < 365 else f"{n // 365}y ago")


def page(title, body, active, extra_css="", extra_js="", wide=False):
    nav = "".join(
        f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>'
        for h, t in (("index.html", "Daily"), ("weekly.html", "This week"),
                     ("explore.html", "Explore"))
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}{extra_css}</style></head>
<body><div class="wrap{' wide' if wide else ''}">
<nav>{nav}</nav>
{body}
<footer>Addon Scout · data from the CurseForge API</footer>
</div>{extra_js}</body></html>"""


# ------------------------------------------------------------------ shared
def family_map(conn):
    parent = {}
    for r in q(conn, """SELECT d.addon_id, d.depends_on_id FROM dependencies d
                        JOIN addons a ON a.id = d.depends_on_id
                        WHERE d.relation_type = 3"""):
        parent.setdefault(r["addon_id"], r["depends_on_id"])

    def root(a, depth=0):
        p = parent.get(a)
        return a if p is None or p == a or depth > 4 else root(p, depth + 1)

    return {a: root(a) for a in parent}


def pick(rows, seen, fams, limit=10, per_author=1, per_family=1):
    out, au, fa = [], {}, {}
    for r in rows:
        i = r["addon_id"]
        if i in seen:
            continue
        a = r["author_name"] or f"?{i}"
        f = fams.get(i, i)
        if au.get(a, 0) >= per_author or fa.get(f, 0) >= per_family:
            continue
        au[a] = au.get(a, 0) + 1
        fa[f] = fa.get(f, 0) + 1
        out.append(r)
        seen.add(i)
        if len(out) >= limit:
            break
    return out


def bars(rows, key, fmt, accent=ACCENT, empty="Nothing to show yet."):
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    top = max((r[key] or 0) for r in rows) or 1
    li = []
    for r in rows:
        v = r[key] or 0
        k = r.keys()
        logo = r["logo_url"] if "logo_url" in k else None
        art = (f'<img class="logo" src="{esc(logo)}" alt="" loading="lazy"'
               f" onerror=\"this.style.visibility='hidden'\">" if logo
               else '<div class="logo"></div>')
        s = r["summary"] if "summary" in k else None
        if s and len(s) > 115:
            s = s[:112].rstrip() + "…"
        meta = [x for x in (
            r["author_name"] if "author_name" in k else None,
            ago(r["date_created"]) if "date_created" in k else None,
        ) if x]
        slug = r["slug"] if "slug" in k else None
        nm = (f'<a href="{CF}{esc(slug)}">{esc(r["name"])}</a>'
              if slug else esc(r["name"]))
        li.append(f"""<li>{art}<div class="bd">
<div class="row-head"><span class="nm">{nm}</span><span class="val">{fmt(v)}</span></div>
{f'<div class="desc">{esc(s)}</div>' if s else ''}
<div class="track"><div class="fill" style="width:{max(1.5, 100 * v / top):.1f}%;background:{accent}"></div></div>
<div class="meta">{esc(' · '.join(meta))}</div></div></li>""")
    return f'<ol class="bars">{"".join(li)}</ol>'


def card(title, sub, body):
    return (f'<section class="card"><h2>{esc(title)}</h2>'
            f'<p class="sub">{esc(sub)}</p>{body}</section>')


def runs_of(conn):
    return q(conn, "SELECT * FROM runs WHERE status='ok' ORDER BY id DESC")


def compare_run(runs, cur, days):
    """Newest full run at least `days` old (with slack for scheduler jitter)."""
    full = [r for r in runs[1:] if r["addons_seen"] >= 0.9 * cur["addons_seen"]]
    want = max(days - 0.25, 0)
    for r in full:
        gap = (datetime.fromisoformat(cur["started_at"])
               - datetime.fromisoformat(r["started_at"])).total_seconds() / 86400
        if gap >= want:
            return r
    return full[-1] if full else None


GAIN_SQL = """
    SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
           ad.logo_url, ad.date_created, a.tier,
           a.project_downloads AS dl,
           a.project_downloads - b.project_downloads AS gain,
           1.0 * (a.project_downloads - b.project_downloads)
               / NULLIF(b.project_downloads, 0) AS rate
    FROM snapshots a
    JOIN snapshots b ON b.addon_id = a.addon_id AND b.run_id = ?
    JOIN addons ad ON ad.id = a.addon_id
    WHERE a.run_id = ? AND a.project_downloads > b.project_downloads"""


def span_text(cur, prev):
    s = (datetime.fromisoformat(cur["started_at"])
         - datetime.fromisoformat(prev["started_at"])).total_seconds()
    return f"{s / 86400:.1f} days" if s >= 86400 else f"{s / 3600:.1f} hours"


# ------------------------------------------------------------------- daily
def build_index(conn, runs):
    cur = runs[0]
    prev = compare_run(runs, cur, 1)
    fams, seen, cards = family_map(conn), set(), []

    tiers = {r["tier"]: r["n"] for r in q(
        conn, "SELECT tier, COUNT(*) n FROM snapshots WHERE run_id=? GROUP BY tier",
        (cur["id"],))}
    newc = q(conn, "SELECT COUNT(*) n FROM addons WHERE first_seen_run=?",
             (cur["id"],))[0]["n"] if prev else 0

    if prev:
        sp = span_text(cur, prev)
        g = q(conn, GAIN_SQL + " ORDER BY gain DESC LIMIT 80", (prev["id"], cur["id"]))
        cards.append(card("🔥 Most downloaded",
                          f"Raw gain over {sp} — the giants win this one. "
                          "One per author, one per family.",
                          bars(pick(g, seen, fams), "gain", lambda v: f"+{v:,}")))

        f = q(conn, GAIN_SQL + """ AND b.project_downloads >= 500
              ORDER BY rate DESC LIMIT 80""", (prev["id"], cur["id"]))
        cards.append(card("🚀 Fastest growing",
                          f"% growth over {sp}, among addons with 500+ downloads.",
                          bars(pick(f, seen, fams), "rate",
                               lambda v: f"+{v:.0%}" if v >= .1 else f"+{v * 100:.1f}%")))

        gem = q(conn, GAIN_SQL + """ AND a.tier='native' AND a.project_downloads < 20000
                ORDER BY gain DESC LIMIT 80""", (prev["id"], cur["id"]))
        cards.append(card("💎 Hidden gems",
                          "Under 20k lifetime downloads, growing fastest right now.",
                          bars(pick(gem, seen, fams), "gain",
                               lambda v: f"+{v:,}", ACCENT_2)))
        cards.append(family_card(conn, cur, prev, fams))
    else:
        cards.append(card("📈 Movement", "Needs two collector runs.",
                          '<p class="empty">Run collect.py again tomorrow.</p>'))

    stats = f"""<div class="stats">
<div><b>{sum(tiers.values()):,}</b><span>Forever addons</span></div>
<div><b>{tiers.get('native', 0):,}</b><span>Forever-native</span></div>
<div><b>{tiers.get('crossflavor', 0):,}</b><span>cross-flavor</span></div>
{f'<div><b>+{newc:,}</b><span>new since last run</span></div>' if prev else ''}</div>"""

    body = (f'<h1>Daily — what moved</h1><p class="when">'
            f'{datetime.now().strftime("%d %b %Y, %H:%M")}</p>{stats}'
            + "".join(cards))
    return page("Addon Scout — Daily", body, "index.html")


def family_card(conn, cur, prev, fams):
    gains = q(conn, GAIN_SQL, (prev["id"], cur["id"]))
    roots = {}
    for r in gains:
        root = fams.get(r["addon_id"], r["addon_id"])
        f = roots.setdefault(root, {"gain": 0, "n": 0, "head": None, "au": []})
        f["gain"] += r["gain"] or 0
        f["n"] += 1
        f["au"].append(r["author_name"])
        if r["addon_id"] == root:
            f["head"] = r
    rows = []
    for root, f in roots.items():
        h = f["head"]
        if f["n"] < 3 or not h:
            continue
        # Only sum one author's own suite. A shared library (Ace3: 29
        # dependents, none by its author) is not one product.
        if sum(1 for a in f["au"] if a and a == h["author_name"]) * 2 < f["n"]:
            continue
        rows.append({"addon_id": root, "name": h["name"], "slug": h["slug"],
                     "author_name": h["author_name"], "logo_url": h["logo_url"],
                     "summary": f"{f['n']} addons in this family moved together",
                     "gain": f["gain"]})
    rows.sort(key=lambda r: -r["gain"])
    return card("🏛️ Hottest families",
                "One author's module suite, summed. A base's own growth "
                "understates it — most module installs come from people who "
                "already had the base. Shared libraries excluded.",
                bars(rows[:8], "gain", lambda v: f"+{v:,}", ACCENT_2,
                     empty="No families with 3+ moving members yet."))


# ------------------------------------------------------------------ weekly
WEEKLY_CSS = """
.wk { background:#1a1a19; border:1px solid #2c2c2a; border-radius:14px;
      padding:26px 28px 14px; }
.wk h1 { font-size:27px; }
.wk .lede { color:#a8a79e; font-size:14px; margin:0 0 22px; }
.wk h2 { font-size:15px; text-transform:uppercase; letter-spacing:.07em;
         color:#8f8e86; margin:22px 0 12px; }
.wk ol.bars li { margin-bottom:14px; }
.tag { display:inline-block; background:#262624; color:#c3c2b7; font-size:11px;
       padding:2px 8px; border-radius:20px; margin-left:6px; vertical-align:middle; }
"""


def build_weekly(conn, runs):
    """One screenshot. Fewer items, more context, no duplication."""
    cur = runs[0]
    prev = compare_run(runs, cur, 7)
    if not prev:
        body = ('<div class="wk"><h1>This week</h1>'
                '<p class="lede">Not enough history yet — needs a second run.</p></div>')
        return page("Addon Scout — This week", body, "weekly.html")

    fams, seen = family_map(conn), set()
    days = (datetime.fromisoformat(cur["started_at"])
            - datetime.fromisoformat(prev["started_at"])).total_seconds() / 86400
    label = (f'{datetime.fromisoformat(prev["started_at"]).strftime("%d %b")} – '
             f'{datetime.fromisoformat(cur["started_at"]).strftime("%d %b %Y")}')

    gems = pick(q(conn, GAIN_SQL + """ AND a.tier='native'
                  AND a.project_downloads < 25000 ORDER BY gain DESC LIMIT 60""",
                  (prev["id"], cur["id"])), seen, fams, limit=5)
    # first_seen_run means "new to OUR data", which is NOT "new addon" — a
    # 16-year-old addon entering the Forever-filtered search for the first
    # time is a PORT. Two different stories, so two queries.
    SEEN_SQL = """
        SELECT ad.id AS addon_id, ad.name, ad.slug, ad.author_name, ad.summary,
               ad.logo_url, ad.date_created, s.project_downloads AS dl
        FROM snapshots s JOIN addons ad ON ad.id = s.addon_id
        WHERE s.run_id = ? AND ad.first_seen_run > ?"""

    fresh = pick(q(conn, SEEN_SQL + """
        AND ad.date_created >= date('now', '-30 days')
        ORDER BY dl DESC LIMIT 60""", (cur["id"], prev["id"])),
        seen, fams, limit=3)

    ports = pick(q(conn, SEEN_SQL + """
        AND ad.date_created < date('now', '-180 days')
        ORDER BY dl DESC LIMIT 60""", (cur["id"], prev["id"])),
        seen, fams, limit=3)

    total = q(conn, "SELECT COUNT(*) n FROM snapshots WHERE run_id=?",
              (cur["id"],))[0]["n"]
    added = q(conn, "SELECT COUNT(*) n FROM addons WHERE first_seen_run > ?",
              (prev["id"],))[0]["n"]

    body = f"""<div class="wk">
<h1>WoW Forever · Addons worth a look</h1>
<p class="lede">{esc(label)} &nbsp;·&nbsp; {total:,} addons tracked &nbsp;·&nbsp;
<b>{added:,} new to Forever</b> &nbsp;·&nbsp; ranked by growth, not lifetime downloads</p>

<h2>Rising, and still small</h2>
{bars(gems, "gain", lambda v: f"+{v:,}", ACCENT_2,
      empty="Nothing under the threshold moved.")}

<h2>Brand new</h2>
{bars(fresh, "dl", lambda v: f"{v:,}", ACCENT,
      empty="No newly created addons this period.")}

<h2>Just added Forever support</h2>
{bars(ports, "dl", lambda v: f"{v:,} lifetime", ACCENT,
      empty="No established addons ported this period.")}
</div>"""
    return page("Addon Scout — This week", body, "weekly.html",
                extra_css=WEEKLY_CSS)


# ----------------------------------------------------------------- explore
EXPLORE_CSS = """
.bar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
.bar input, .bar select { background:#1a1a19; color:#f0efec; border:1px solid #2c2c2a;
  border-radius:8px; padding:8px 11px; font:14px inherit; }
.bar input { flex:1 1 260px; }
table { width:100%; border-collapse:collapse; font-size:13.5px; }
th { text-align:left; color:#8f8e86; font-weight:500; font-size:12px; padding:6px 8px;
  border-bottom:1px solid #2c2c2a; cursor:pointer; user-select:none; white-space:nowrap; }
th:hover { color:#f0efec; }
td { padding:7px 8px; border-bottom:1px solid #1f1f1d; vertical-align:middle; }
tr:hover td { background:#1a1a19; }
td.n { text-align:right; font-variant-numeric:tabular-nums; }
td img { width:26px; height:26px; border-radius:6px; object-fit:cover; background:#262624;
  vertical-align:middle; margin-right:8px; }
.t.fam { cursor:pointer; margin-left:7px; background:#2d3b4d; color:#a9c5e8; }
.t { font-size:10.5px; padding:1px 7px; border-radius:20px; background:#262624; color:#a8a79e; }
.count { color:#8f8e86; font-size:12.5px; margin:10px 0 0; }
dialog { background:#1a1a19; color:#f0efec; border:1px solid #2c2c2a; border-radius:14px;
  max-width:560px; width:92%; padding:22px 24px; }
dialog::backdrop { background:rgba(0,0,0,.6); }
dialog h3 { margin:0 0 4px; font-size:19px; }
dialog .k { color:#8f8e86; font-size:12px; }
dialog table { margin-top:12px; }
dialog .sib { color:#3987e5; text-decoration:none; }
dialog .sib:hover { text-decoration:underline; }
.spark { display:flex; align-items:flex-end; gap:3px; height:46px; margin:14px 0 4px; }
.spark div { flex:1; background:#3987e5; border-radius:3px 3px 0 0; min-height:2px; }
"""


def build_explore(conn, runs, out):
    cur = runs[0]
    prev = compare_run(runs, cur, 1)
    hist_runs = [r["id"] for r in reversed(runs[:14])]

    fams = family_map(conn)
    names = {r["id"]: r["name"] for r in q(conn, "SELECT id, name FROM addons")}
    auth = {r["id"]: r["author_name"] for r in q(
        conn, "SELECT id, author_name FROM addons")}
    members = {}
    for aid, root in fams.items():
        members.setdefault(root, set()).add(aid)
    for root in list(members):
        members[root].add(root)          # the parent belongs to its own family
    # Only families worth offering as a filter, and only single-author suites
    # — a shared library's "members" are unrelated addons that merely use it.
    famidx = {}
    for root, ms in members.items():
        if len(ms) < 2 or root not in names:
            continue
        same = sum(1 for m in ms if auth.get(m) and auth.get(m) == auth.get(root))
        famidx[root] = {"n": names[root], "c": len(ms),
                        "suite": 1 if same * 2 >= len(ms) else 0}

    cats = {r["id"]: r["name"] for r in q(
        conn, "SELECT id, name FROM categories WHERE is_class=0")}
    acats = {}
    for r in q(conn, "SELECT addon_id, category_id FROM addon_categories"):
        acats.setdefault(r["addon_id"], []).append(r["category_id"])

    hist = {}
    for r in q(conn, f"""SELECT addon_id, run_id, project_downloads FROM snapshots
                         WHERE run_id IN ({','.join('?' * len(hist_runs))})""",
               hist_runs):
        hist.setdefault(r["addon_id"], {})[r["run_id"]] = r["project_downloads"]

    prev_dl = {}
    if prev:
        for r in q(conn, "SELECT addon_id, project_downloads FROM snapshots WHERE run_id=?",
                   (prev["id"],)):
            prev_dl[r["addon_id"]] = r["project_downloads"]

    rows = []
    for r in q(conn, """
        SELECT ad.id, ad.name, ad.slug, ad.author_name, ad.summary, ad.logo_url,
               ad.date_created, ad.source_url, s.tier, s.project_downloads dl,
               s.forever_only_downloads fod, s.forever_files ff, s.date_modified dm
        FROM snapshots s JOIN addons ad ON ad.id = s.addon_id
        WHERE s.run_id = ?""", (cur["id"],)):
        h = hist.get(r["id"], {})
        rows.append({
            "i": r["id"], "n": r["name"], "s": r["slug"], "a": r["author_name"],
            "d": (r["summary"] or "")[:180], "l": r["logo_url"], "t": r["tier"],
            "dl": r["dl"] or 0, "g": (r["dl"] or 0) - prev_dl.get(r["id"], r["dl"] or 0),
            "c": (r["date_created"] or "")[:10], "m": (r["dm"] or "")[:10],
            "fod": r["fod"] or 0, "ff": r["ff"] or 0, "src": r["source_url"],
            "cat": acats.get(r["id"], []),
            "f": fams.get(r["id"]),          # family root, or None
            "h": [h.get(rid) for rid in hist_runs],
        })

    data = {"generated": datetime.now().isoformat(timespec="seconds"),
            "cats": cats, "fams": famidx, "addons": rows,
            "runs": [r["started_at"][:10] for r in reversed(runs[:14])]}
    blob = json.dumps(data, separators=(",", ":"))
    # Also written as a standalone file for any other tool that wants it.
    (out / "data.json").write_text(blob, encoding="utf-8")

    opts = "".join(f'<option value="{i}">{esc(n)}</option>'
                   for i, n in sorted(cats.items(), key=lambda kv: kv[1]))
    fopts = "".join(
        f'<option value="{i}">{esc(f["n"])} ({f["c"]})</option>'
        for i, f in sorted(famidx.items(),
                           key=lambda kv: (-kv[1]["suite"], kv[1]["n"].lower()))
        if f["suite"])
    body = f"""<h1>Explore</h1>
<p class="when">All {len(rows):,} tracked addons. Everything below runs in your
browser — type to search, click a column to sort, click a row for detail.</p>
<div class="bar">
  <input id="qq" placeholder="Search name, author or description…" autocomplete="off">
  <select id="tier"><option value="">Any tier</option>
    <option value="native">Forever-native</option>
    <option value="crossflavor">Cross-flavor</option></select>
  <select id="cat"><option value="">Any category</option>{opts}</select>
  <select id="age"><option value="">Any age</option>
    <option value="7">Created last 7 days</option>
    <option value="30">Created last 30 days</option></select>
  <select id="fam"><option value="">Any family</option>{fopts}</select>
</div>
<table><thead><tr>
<th data-k="n">Addon</th><th data-k="a">Author</th><th data-k="t">Tier</th>
<th data-k="dl" class="n">Downloads</th><th data-k="g" class="n">Change</th>
<th data-k="c" class="n">Created</th></tr></thead><tbody id="tb"></tbody></table>
<p class="count" id="cnt"></p>
<dialog id="dlg"></dialog>"""

    # The data is INLINED, not fetched. A page opened from disk runs on
    # file://, where fetch() is blocked by CORS — explore.html would show an
    # empty table and no error. Inlining keeps "just double-click it" true.
    js = """<script id="d" type="application/json">__DATA__</script>
<script>
const S = {q:"", tier:"", cat:"", age:"", fam:"", k:"dl", dir:-1};
const J = JSON.parse(document.getElementById("d").textContent);
const D = J.addons, CAT = J.cats, RUNS = J.runs, FAM = J.fams;

const esc = s => (s ?? "").toString().replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = n => (n ?? 0).toLocaleString();

function filtered() {
  const q = S.q.toLowerCase();
  const cut = S.age ? Date.now() - S.age * 864e5 : null;
  return D.filter(a => {
    if (S.tier && a.t !== S.tier) return false;
    if (S.cat && !a.cat.includes(+S.cat)) return false;
    if (S.fam && a.f != S.fam && a.i != S.fam) return false;
    if (cut && !(a.c && Date.parse(a.c) >= cut)) return false;
    if (q && !((a.n + " " + (a.a || "") + " " + a.d).toLowerCase().includes(q)))
      return false;
    return true;
  }).sort((x, y) => {
    const A = x[S.k], B = y[S.k];
    return (typeof A === "string" ? A.localeCompare(B) : (A || 0) - (B || 0)) * S.dir;
  });
}

function render() {
  const rows = filtered();
  document.getElementById("tb").innerHTML = rows.slice(0, 400).map((a, i) => `
    <tr data-i="${a.i}">
      <td>${a.l ? `<img src="${esc(a.l)}" loading="lazy" onerror="this.style.visibility='hidden'">` : ""}${esc(a.n)}${
        FAM[a.i] && FAM[a.i].suite ? `<span class="t fam" data-f="${a.i}" title="Show this family">suite of ${FAM[a.i].c}</span>` : ""}</td>
      <td>${esc(a.a)}</td><td><span class="t">${esc(a.t)}</span></td>
      <td class="n">${num(a.dl)}</td>
      <td class="n">${a.g > 0 ? "+" + num(a.g) : ""}</td>
      <td class="n">${esc(a.c)}</td>
    </tr>`).join("");
  document.getElementById("cnt").textContent =
    rows.length > 400 ? `Showing 400 of ${num(rows.length)} matches`
                      : `${num(rows.length)} addons`;
}

document.getElementById("qq").oninput = e => { S.q = e.target.value; render(); };
for (const [id, key] of [["tier","tier"],["cat","cat"],["age","age"],["fam","fam"]])
  document.getElementById(id).onchange = e => { S[key] = e.target.value; render(); };

document.querySelectorAll("th").forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  S.dir = S.k === k ? -S.dir : (k === "n" || k === "a" ? 1 : -1);
  S.k = k; render();
});

document.getElementById("tb").onclick = e => {
  const badge = e.target.closest(".fam");
  if (badge) {
    S.fam = badge.dataset.f;
    document.getElementById("fam").value = S.fam;
    render();
    return;
  }
  const tr = e.target.closest("tr"); if (!tr) return;
  const a = D.find(x => x.i == tr.dataset.i); if (!a) return;
  openDetail(a);
};

function openDetail(a) {
  const h = a.h.filter(v => v != null);
  const lo = Math.min(...h), hi = Math.max(...h), rng = hi - lo || 1;
  const spark = h.length > 1 ? `<div class="spark">${
    h.map(v => `<div style="height:${8 + 92 * (v - lo) / rng}%" title="${num(v)}"></div>`)
     .join("")}</div><div class="k">${RUNS[0]} → ${RUNS[RUNS.length-1]} · ${num(lo)} → ${num(hi)}</div>` : "";
  document.getElementById("dlg").innerHTML = `
    <h3>${esc(a.n)}</h3>
    <div class="k">${esc(a.a)} · created ${esc(a.c)} · updated ${esc(a.m)}</div>
    <p style="font-size:13.5px;color:#a8a79e">${esc(a.d)}</p>
    ${spark}
    <table><tbody>
      <tr><td class="k">Tier</td><td>${esc(a.t)}</td></tr>
      <tr><td class="k">Lifetime downloads</td><td>${num(a.dl)}</td></tr>
      <tr><td class="k">Forever-only downloads</td><td>${num(a.fod)}${a.fod ? "" : " <span class='k'>(all files are multi-flavor)</span>"}</td></tr>
      <tr><td class="k">Forever files</td><td>${num(a.ff)}</td></tr>
      <tr><td class="k">Categories</td><td>${a.cat.map(c => esc(CAT[c])).filter(Boolean).join(", ")}</td></tr>
      ${famRow(a)}
    </tbody></table>
    <p style="margin-top:16px">
      <a href="https://www.curseforge.com/wow/addons/${esc(a.s)}">CurseForge ↗</a>
      ${a.src ? ` · <a href="${esc(a.src)}">Source ↗</a>` : ""}
    </p>`;
  document.getElementById("dlg").showModal();
}

function famRow(a) {
  const root = a.f || (FAM[a.i] ? a.i : null);
  if (!root || !FAM[root]) return "";
  const sibs = D.filter(x => (x.f === root || x.i == root) && x.i != a.i)
                .sort((x, y) => y.dl - x.dl);
  if (!sibs.length) return "";
  const head = a.i == root ? "Modules" : `Part of ${esc(FAM[root].n)}`;
  return `<tr><td class="k">${head}</td><td>${
    sibs.slice(0, 12).map(x => `<a href="#" class="sib" data-i="${x.i}">${esc(x.n)}</a>`).join(", ")
  }${sibs.length > 12 ? ` <span class="k">+${sibs.length - 12} more</span>` : ""}</td></tr>`;
}

// jump between siblings without closing the dialog
document.getElementById("dlg").addEventListener("click", e => {
  const sib = e.target.closest(".sib");
  if (!sib) return;
  e.preventDefault();
  const a = D.find(x => x.i == sib.dataset.i);
  if (a) openDetail(a);
});

document.getElementById("dlg").onclick = e => {
  if (e.target.id === "dlg") e.target.close();
};
</script>"""
    # </script> inside the payload would close the tag early.
    js = js.replace("__DATA__", blob.replace("</", "<\\/"))
    js += "<script>render();</script>"
    return page("Addon Scout — Explore", body, "explore.html",
                extra_css=EXPLORE_CSS, extra_js=js, wide=True)


# -------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB))
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--no-open", action="store_true")
    a = p.parse_args()

    dbp = Path(a.db)
    if not dbp.exists():
        sys.exit(f"No database at {dbp.resolve()} — run collect.py first.")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    runs = runs_of(conn)
    if not runs:
        sys.exit("No completed runs yet.")

    (out / "index.html").write_text(build_index(conn, runs), encoding="utf-8")
    (out / "weekly.html").write_text(build_weekly(conn, runs), encoding="utf-8")
    (out / "explore.html").write_text(build_explore(conn, runs, out), encoding="utf-8")
    conn.close()

    size = (out / "data.json").stat().st_size / 1_048_576
    print(f"Built {out.resolve()}  (data.json {size:.1f} MB)")
    if not a.no_open:
        webbrowser.open((out / "index.html").resolve().as_uri())


if __name__ == "__main__":
    main()
