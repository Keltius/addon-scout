"""
SQLite storage for Addon Scout.

Design rule: store raw inputs, derive everything else later. Project totals
and file totals measure different things (36%-86% of each other), so both
are recorded and never mixed in one formula. See the vault's Decisions Log.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).with_name("addon_scout.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',   -- running|ok|failed|interrupted
    last_index  INTEGER NOT NULL DEFAULT 0,        -- for resuming
    addons_seen INTEGER NOT NULL DEFAULT 0,
    api_calls   INTEGER NOT NULL DEFAULT 0,
    mode        TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    slug     TEXT,
    class_id INTEGER,
    is_class INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS addons (
    id                  INTEGER PRIMARY KEY,       -- CurseForge mod id
    slug                TEXT,
    name                TEXT,
    summary             TEXT,
    class_id            INTEGER,
    primary_category_id INTEGER,
    author_id           INTEGER,
    author_name         TEXT,
    website_url         TEXT,
    source_url          TEXT,                      -- GitHub, when the author links it
    issues_url          TEXT,
    date_created        TEXT,
    date_released       TEXT,
    date_modified       TEXT,
    allow_distribution  INTEGER,
    logo_url            TEXT,                      -- media.forgecdn.net thumbnail
    first_seen_run      INTEGER REFERENCES runs(id),
    last_seen_run       INTEGER REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS addon_categories (
    addon_id    INTEGER NOT NULL REFERENCES addons(id),
    category_id INTEGER NOT NULL,
    PRIMARY KEY (addon_id, category_id)
);

CREATE TABLE IF NOT EXISTS files (
    id               INTEGER PRIMARY KEY,          -- CurseForge file id
    addon_id         INTEGER NOT NULL REFERENCES addons(id),
    display_name     TEXT,
    file_name        TEXT,
    file_date        TEXT,
    file_length      INTEGER,
    release_type     INTEGER,
    is_forever       INTEGER NOT NULL DEFAULT 0,
    is_multi_flavor  INTEGER NOT NULL DEFAULT 0,   -- Forever AND another flavor
    version_type_ids TEXT,                         -- JSON array
    game_versions    TEXT,                         -- JSON array
    first_seen_run   INTEGER REFERENCES runs(id)
);

-- Declared relationships between addons, straight from each file's
-- `dependencies` array. Free: we already fetch it and were discarding it.
-- depends_on_id has no FK — it often points at an addon outside the Forever
-- set (a Retail-only library, say), which we never collect.
CREATE TABLE IF NOT EXISTS dependencies (
    addon_id       INTEGER NOT NULL REFERENCES addons(id),
    depends_on_id  INTEGER NOT NULL,
    relation_type  INTEGER NOT NULL,   -- 1 lib 2 optional 3 required 4 tool 5 incompatible 6 include
    first_seen_run INTEGER,
    last_seen_run  INTEGER,
    PRIMARY KEY (addon_id, depends_on_id, relation_type)
);

-- ===================== HAND-CURATED — THE COLLECTOR NEVER WRITES HERE =====
-- For relationships no API field expresses: addons that compete with each
-- other rather than depend on each other (Node Sight / Lueur / Forever Loot
-- Sparkles all light up gathering nodes, and none declares any link to the
-- others). Edit these by hand in SQLiteStudio.
CREATE TABLE IF NOT EXISTS curated_groups (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,         -- e.g. "Gathering node highlighters"
    note TEXT
);
CREATE TABLE IF NOT EXISTS curated_group_members (
    group_id INTEGER NOT NULL REFERENCES curated_groups(id),
    addon_id INTEGER NOT NULL,
    note     TEXT,
    PRIMARY KEY (group_id, addon_id)
);
-- ==========================================================================

-- One row per addon per run. The time series that makes velocity possible.
CREATE TABLE IF NOT EXISTS snapshots (
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    addon_id INTEGER NOT NULL REFERENCES addons(id),
    taken_at TEXT NOT NULL,

    project_downloads INTEGER,   -- lifetime, ALL flavors. Not comparable to file sums.
    thumbs_up         INTEGER,
    rating            REAL,
    popularity_rank   INTEGER,
    date_modified     TEXT,

    tier               TEXT,     -- native|crossflavor|unknown
    forever_files      INTEGER,  -- files tagged Forever (any)
    forever_only_files INTEGER,  -- files tagged Forever and NOTHING else

    -- Sum over Forever-tagged files. Includes multi-flavor files, so this
    -- is NOT attributable to Forever. Kept for comparison only.
    forever_any_downloads  INTEGER,
    -- Sum over Forever-exclusive files. This one IS attributable.
    forever_only_downloads INTEGER,

    PRIMARY KEY (run_id, addon_id)
);

CREATE TABLE IF NOT EXISTS file_snapshots (
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    file_id   INTEGER NOT NULL REFERENCES files(id),
    taken_at  TEXT NOT NULL,
    downloads INTEGER,
    PRIMARY KEY (run_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_snap_addon  ON snapshots(addon_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_fsnap_file  ON file_snapshots(file_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_files_addon ON files(addon_id);
CREATE INDEX IF NOT EXISTS idx_addons_slug ON addons(slug);
CREATE INDEX IF NOT EXISTS idx_deps_target  ON dependencies(depends_on_id);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DEFAULT_DB):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns to an existing database without losing its data.
    CREATE TABLE IF NOT EXISTS won't alter a table that already exists."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(addons)")}
    for col, decl in [("logo_url", "TEXT")]:
        if col not in have:
            conn.execute(f"ALTER TABLE addons ADD COLUMN {col} {decl}")
    conn.commit()


# ----------------------------------------------------------------- runs
def start_run(conn, mode):
    cur = conn.execute(
        "INSERT INTO runs (started_at, mode) VALUES (?, ?)", (now_iso(), mode)
    )
    conn.commit()
    return cur.lastrowid


def update_run(conn, run_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()


def finish_run(conn, run_id, status, api_calls, addons_seen, note=None):
    update_run(
        conn, run_id,
        finished_at=now_iso(), status=status,
        api_calls=api_calls, addons_seen=addons_seen, note=note,
    )


def previous_run(conn, before_run_id):
    row = conn.execute(
        "SELECT * FROM runs WHERE id < ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (before_run_id,),
    ).fetchone()
    return row


# ----------------------------------------------------------- categories
def save_categories(conn, categories):
    conn.executemany(
        """INSERT INTO categories (id, name, slug, class_id, is_class)
           VALUES (:id, :name, :slug, :class_id, :is_class)
           ON CONFLICT(id) DO UPDATE SET
             name = excluded.name, slug = excluded.slug,
             class_id = excluded.class_id, is_class = excluded.is_class""",
        [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "slug": c.get("slug"),
                "class_id": c.get("classId"),
                "is_class": 1 if c.get("isClass") else 0,
            }
            for c in categories
        ],
    )
    conn.commit()


# --------------------------------------------------------------- addons
def save_addon(conn, run_id, mod):
    links = mod.get("links") or {}
    authors = mod.get("authors") or []
    author = authors[0] if authors else {}

    conn.execute(
        """INSERT INTO addons (
               id, slug, name, summary, class_id, primary_category_id,
               author_id, author_name, website_url, source_url, issues_url,
               date_created, date_released, date_modified, allow_distribution,
               logo_url, first_seen_run, last_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               slug=excluded.slug, name=excluded.name, summary=excluded.summary,
               class_id=excluded.class_id,
               primary_category_id=excluded.primary_category_id,
               author_id=excluded.author_id, author_name=excluded.author_name,
               website_url=excluded.website_url, source_url=excluded.source_url,
               issues_url=excluded.issues_url,
               date_created=excluded.date_created,
               date_released=excluded.date_released,
               date_modified=excluded.date_modified,
               allow_distribution=excluded.allow_distribution,
               logo_url=excluded.logo_url,
               last_seen_run=excluded.last_seen_run""",
        (
            mod.get("id"), mod.get("slug"), mod.get("name"), mod.get("summary"),
            mod.get("classId"), mod.get("primaryCategoryId"),
            author.get("id"), author.get("name"),
            links.get("websiteUrl"), links.get("sourceUrl"), links.get("issuesUrl"),
            mod.get("dateCreated"), mod.get("dateReleased"), mod.get("dateModified"),
            1 if mod.get("allowModDistribution") else 0,
            (mod.get("logo") or {}).get("thumbnailUrl"),
            run_id, run_id,
        ),
    )

    cats = [c.get("id") for c in (mod.get("categories") or []) if c.get("id")]
    if cats:
        conn.executemany(
            "INSERT OR IGNORE INTO addon_categories (addon_id, category_id) VALUES (?, ?)",
            [(mod.get("id"), c) for c in cats],
        )


def save_file(conn, run_id, addon_id, f, forever_type_id, taken_at):
    types = sorted({
        s.get("gameVersionTypeId")
        for s in (f.get("sortableGameVersions") or [])
        if s.get("gameVersionTypeId") is not None
    })
    is_forever = forever_type_id in types
    is_multi = is_forever and len(types) > 1

    conn.execute(
        """INSERT INTO files (
               id, addon_id, display_name, file_name, file_date, file_length,
               release_type, is_forever, is_multi_flavor, version_type_ids,
               game_versions, first_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               display_name=excluded.display_name, file_name=excluded.file_name,
               file_date=excluded.file_date, file_length=excluded.file_length,
               release_type=excluded.release_type,
               is_forever=excluded.is_forever,
               is_multi_flavor=excluded.is_multi_flavor,
               version_type_ids=excluded.version_type_ids,
               game_versions=excluded.game_versions""",
        (
            f.get("id"), addon_id, f.get("displayName"), f.get("fileName"),
            f.get("fileDate"), f.get("fileLength"), f.get("releaseType"),
            1 if is_forever else 0, 1 if is_multi else 0,
            json.dumps(types), json.dumps(f.get("gameVersions") or []),
            run_id,
        ),
    )
    conn.execute(
        """INSERT INTO file_snapshots (run_id, file_id, taken_at, downloads)
           VALUES (?,?,?,?)
           ON CONFLICT(run_id, file_id) DO UPDATE SET downloads=excluded.downloads""",
        (run_id, f.get("id"), taken_at, f.get("downloadCount")),
    )
    return is_forever, is_multi


def save_dependencies(conn, run_id, addon_id, f):
    """Record what this file declares it needs. Upsert, never delete: an
    author dropping a dependency shouldn't erase that it once existed."""
    deps = f.get("dependencies") or []
    if not deps:
        return 0
    rows = [
        (addon_id, d.get("modId"), d.get("relationType"), run_id, run_id)
        for d in deps
        if d.get("modId") and d.get("relationType")
    ]
    conn.executemany(
        """INSERT INTO dependencies
               (addon_id, depends_on_id, relation_type, first_seen_run, last_seen_run)
           VALUES (?,?,?,?,?)
           ON CONFLICT(addon_id, depends_on_id, relation_type)
           DO UPDATE SET last_seen_run = excluded.last_seen_run""",
        rows,
    )
    return len(rows)


def save_snapshot(conn, run_id, addon_id, taken_at, mod, stats):
    conn.execute(
        """INSERT INTO snapshots (
               run_id, addon_id, taken_at, project_downloads, thumbs_up, rating,
               popularity_rank, date_modified, tier, forever_files,
               forever_only_files, forever_any_downloads, forever_only_downloads)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id, addon_id) DO UPDATE SET
               project_downloads=excluded.project_downloads,
               thumbs_up=excluded.thumbs_up, rating=excluded.rating,
               popularity_rank=excluded.popularity_rank,
               date_modified=excluded.date_modified, tier=excluded.tier,
               forever_files=excluded.forever_files,
               forever_only_files=excluded.forever_only_files,
               forever_any_downloads=excluded.forever_any_downloads,
               forever_only_downloads=excluded.forever_only_downloads""",
        (
            run_id, addon_id, taken_at, mod.get("downloadCount"),
            mod.get("thumbsUpCount"), mod.get("rating"),
            mod.get("gamePopularityRank"), mod.get("dateModified"),
            stats["tier"], stats["forever_files"], stats["forever_only_files"],
            stats["forever_any_downloads"], stats["forever_only_downloads"],
        ),
    )
