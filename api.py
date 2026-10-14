"""
Thin CurseForge Core API client.

Retry/backoff comes from urllib3's Retry rather than a hand-rolled rate
limiter — the 2023 project's custom RateLimiter class never fired because
it was keyed wrong. Retry handles 429 and honours Retry-After for free.
"""

import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

BASE = "https://api.curseforge.com/v1"

GAME_ID = 1                 # World of Warcraft
FOREVER_TYPE_ID = 88568     # "WoW Forever"
ADDON_CLASS_ID = 1
MAX_PAGE = 50               # API maximum
PAGING_CEILING = 10_000     # index + pageSize must stay <= this


class CurseForgeError(RuntimeError):
    pass


class CurseForge:
    def __init__(self, api_key=None, base=BASE, pause=0.25):
        api_key = api_key or os.environ.get("CURSEFORGE_API_KEY")
        if not api_key:
            raise CurseForgeError(
                "CURSEFORGE_API_KEY is not set.\n"
                '  setx CURSEFORGE_API_KEY "your-key"   then open a NEW terminal'
            )
        self.base = base.rstrip("/")
        self.pause = pause
        self.calls = 0

        retry = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            respect_retry_after_header=True,
        )
        session = requests.Session()
        session.headers.update({
            "x-api-key": api_key,
            "Accept": "application/json",
            "User-Agent": "addon-scout/0.1 (personal analytics; non-commercial)",
        })
        session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session = session

    def get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        resp = self.session.get(f"{self.base}{path}", params=params or None, timeout=30)
        self.calls += 1
        if resp.status_code != 200:
            raise CurseForgeError(
                f"HTTP {resp.status_code} on {path} :: {resp.text[:200]}"
            )
        time.sleep(self.pause)
        return resp.json()

    # ------------------------------------------------------------ lookups
    def categories(self, game_id=GAME_ID):
        return self.get("/categories", gameId=game_id).get("data", [])

    def version_types(self, game_id=GAME_ID):
        return self.get(f"/games/{game_id}/version-types").get("data", [])

    def search_page(self, index, page_size=MAX_PAGE, game_id=GAME_ID,
                    version_type_id=FOREVER_TYPE_ID, sort_field=4, sort_order="asc"):
        """One page of mods. sortField 4 = Name, the most stable ordering for
        a full enumeration (release/update dates shift as authors publish)."""
        if index + page_size > PAGING_CEILING:
            raise CurseForgeError(
                f"Paging ceiling hit: index {index} + {page_size} > {PAGING_CEILING}. "
                "Split the query by category."
            )
        return self.get(
            "/mods/search",
            gameId=game_id,
            gameVersionTypeId=version_type_id,
            sortField=sort_field,
            sortOrder=sort_order,
            index=index,
            pageSize=page_size,
        )

    def mod_files(self, mod_id, version_type_id=FOREVER_TYPE_ID, max_files=1000):
        """All files for a mod, filtered server-side by version type.

        The filter matters: DBM has 400+ files but only ~80 tagged Forever.

        Raises rather than truncating if max_files is hit — a silent cap is
        how verify_downloads.py produced a wrong answer on 2026-10-14.
        """
        out, index = [], 0
        while True:
            batch = self.get(
                f"/mods/{mod_id}/files",
                gameVersionTypeId=version_type_id,
                index=index,
                pageSize=MAX_PAGE,
            ).get("data", [])
            out.extend(batch)
            if len(batch) < MAX_PAGE:
                return out
            index += MAX_PAGE
            if index >= max_files:
                raise CurseForgeError(
                    f"mod {mod_id} has more than {max_files} files; raise max_files "
                    "rather than accepting truncated data"
                )
