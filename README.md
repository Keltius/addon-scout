# Addon Scout

A personal analytics tool for discovering World of Warcraft addons — specifically for **WoW: Forever**.

## The Problem

CurseForge lets you sort addons by popularity, creation date, last update, and total downloads. None of those work well for a newly launched game version:

- **Download counts are lifetime totals across every game flavor.** An addon that spent ten years on Retail and just added a Forever file shows an enormous number that says nothing about its Forever adoption.
- **Totals favor age.** A five-year-old addon will always outrank a three-week-old one, even if the new one is growing ten times faster.
- **Good small addons stay buried.** There's no way to sort by "growing fast relative to its size."

## The Approach

Addon Scout reads public addon metadata from the CurseForge API once a day and stores it as a time series in a local SQLite database. That history is what makes better ranking possible:

- **Forever-specific download counts** — summed from files tagged for Forever, rather than the project's lifetime total
- **Age-adjusted velocity** — downloads per day since the Forever release, not downloads ever
- **Market-share normalization** — an addon's weekly downloads measured against total Forever addon activity that week, so launch-week hype doesn't permanently favor early arrivals
- **Maintenance signals** — has it been updated since the last patch?
- **A separate "gem score"** — rewards growth rate over size, so small rising addons rank on their own terms instead of competing with established giants

It never downloads, hosts, or mirrors addon files. Everything it surfaces links back to the addon's CurseForge page.

## Status

Early development. Currently awaiting CurseForge API access.

**Roadmap:**
1. Data collection — daily snapshots into SQLite
2. Scoring — relevancy and gem scores
3. Reporting — weekly "hidden gems" and biggest movers
4. Compatibility scanning — flag addons using APIs restricted in Forever
5. Additional sources and games

## Non-Commercial

Hobby project. No ads, subscriptions, donations, or paid features.
