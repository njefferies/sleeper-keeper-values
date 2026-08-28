#!/usr/bin/env python3
"""
Sleeper Fantasy Football keeper value calculator.

League formula:
    keeper_value = last_season_drafted_or_waiver_value + (1/3 * this_season_projected_value)

Where:
    - "last_season_drafted_or_waiver_value" is the $ cost the player was actually
      acquired for during LAST season: the auction-draft winning bid, or (if they
      were picked up off waivers/free agency at any point last season) the FAAB
      dollars spent on the most recent successful add. Trades do not change this
      cost -- it always reflects how the player originally entered the league that
      season. A player with no draft/waiver history last season (e.g. brand new
      to the league) is treated as $0.
    - "this_season_projected_value" is Sleeper's own $PROJ auction-value column
      from the draft room -- the actual number Sleeper itself projects each
      player would go for in a $200 auction, not a homemade estimate. Sleeper
      doesn't expose this through any public API (it's rendered client-side in
      the draft room), so it has to be scraped once per season with the
      companion script, sleeper_scrape_draft_values.py, into a CSV that this
      script reads.

Only the standard library is used here -- no `pip install` needed, just
Python 3.8+. (The scraper companion script needs Playwright; see its own
docstring.)

Usage:
    python sleeper_scrape_draft_values.py --login                 # one-time
    python sleeper_scrape_draft_values.py --league-id <league_id> --out draft_values.csv
    python sleeper_keeper_values.py --league-id <league_id> --draft-values draft_values.csv

Find your league_id in the URL when you open your league on sleeper.com, e.g.
sleeper.com/leagues/<league_id>/team.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

API = "https://api.sleeper.app/v1"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sleeper_cache")
PLAYERS_CACHE_PATH = os.path.join(CACHE_DIR, "players.json")
PLAYERS_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # 1 week


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3, backoff: float = 1.5):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "keeper-calc/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# --------------------------------------------------------------------------
# Sleeper data pulls
# --------------------------------------------------------------------------

def get_league(league_id: str) -> dict:
    return fetch_json(f"{API}/league/{league_id}")


def get_rosters(league_id: str) -> list:
    return fetch_json(f"{API}/league/{league_id}/rosters")


def get_users(league_id: str) -> list:
    return fetch_json(f"{API}/league/{league_id}/users")


def get_drafts(league_id: str) -> list:
    return fetch_json(f"{API}/league/{league_id}/drafts")


def get_draft_picks(draft_id: str) -> list:
    return fetch_json(f"{API}/draft/{draft_id}/picks")


def get_transactions(league_id: str, week: int) -> list:
    return fetch_json(f"{API}/league/{league_id}/transactions/{week}")


def get_players(use_cache: bool = True) -> dict:
    """The full NFL player dictionary (player_id -> info). ~5MB, cache it."""
    if use_cache and os.path.exists(PLAYERS_CACHE_PATH):
        age = time.time() - os.path.getmtime(PLAYERS_CACHE_PATH)
        if age < PLAYERS_CACHE_MAX_AGE_SECONDS:
            with open(PLAYERS_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)

    print("Downloading full Sleeper player list (cached for a week)...", file=sys.stderr)
    data = fetch_json(f"{API}/players/nfl")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PLAYERS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


# --------------------------------------------------------------------------
# Value calculation
# --------------------------------------------------------------------------

def build_last_season_values(league_id: str) -> dict:
    """
    Returns {player_id: cost_dollars} representing what each player was
    acquired for at any point during the given (last) season -- draft cost,
    or the FAAB spent on their most recent successful waiver/free-agent add,
    whichever happened most recently. Drops/trades don't erase the cost.
    """
    events = []  # (timestamp, player_id, cost)

    # 1. Draft(s) -- auction winning bids
    for draft in get_drafts(league_id):
        draft_id = draft.get("draft_id")
        draft_ts = draft.get("start_time") or 0
        picks = get_draft_picks(draft_id)
        for pick in picks:
            pid = pick.get("player_id")
            if not pid:
                continue
            meta = pick.get("metadata") or {}
            amount = meta.get("amount")
            try:
                cost = float(amount) if amount not in (None, "") else 0.0
            except (TypeError, ValueError):
                cost = 0.0
            events.append((draft_ts, pid, cost))

    # 2. Waivers / free agency across the whole season (weeks 1-18 covers regular
    #    season + a safety margin; Sleeper just returns [] for weeks with no
    #    transactions so this is cheap/safe to over-request).
    for week in range(1, 19):
        try:
            txns = get_transactions(league_id, week)
        except RuntimeError:
            continue
        for txn in txns:
            if txn.get("status") != "complete":
                continue
            ttype = txn.get("type")
            if ttype not in ("waiver", "free_agent"):
                continue
            adds = txn.get("adds") or {}
            if not adds:
                continue
            settings = txn.get("settings") or {}
            cost = float(settings.get("waiver_bid") or 0) if ttype == "waiver" else 0.0
            created = txn.get("created") or 0
            for pid in adds.keys():
                events.append((created, pid, cost))

    # Apply chronologically so the *most recent* acquisition cost wins.
    events.sort(key=lambda e: e[0])
    values: dict = {}
    for _, pid, cost in events:
        values[pid] = cost
    return values


def load_draft_values(csv_path: str) -> dict:
    """
    Loads Sleeper's own $PROJ auction-value column, produced by
    sleeper_scrape_draft_values.py (there's no public API for this number --
    it's rendered client-side in the draft room -- so it has to be scraped
    once per season and handed to this script as a CSV).

    Returns {player_id: {"dollar": float, "points": float}}.
    """
    if not os.path.exists(csv_path):
        print(
            f"ERROR: {csv_path} not found. Run sleeper_scrape_draft_values.py first "
            f"to produce it (see that script's docstring for the one-time login step).",
            file=sys.stderr,
        )
        sys.exit(1)

    values = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = row.get("player_id")
            if not pid:
                continue
            try:
                dollar = float(row["draft_dollar_value"]) if row.get("draft_dollar_value") else 0.0
            except ValueError:
                dollar = 0.0
            try:
                pts = float(row["draft_projected_points"]) if row.get("draft_projected_points") else 0.0
            except ValueError:
                pts = 0.0
            values[pid] = {"dollar": dollar, "points": pts}
    return values


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Calculate Sleeper league keeper values.")
    parser.add_argument("--league-id", required=True, help="Current season Sleeper league_id")
    parser.add_argument("--draft-values", default="draft_values.csv",
                         help="CSV produced by sleeper_scrape_draft_values.py "
                              "(Sleeper's own $PROJ auction values)")
    parser.add_argument("--out", default="keeper_values.csv", help="Output CSV path")
    parser.add_argument("--no-cache", action="store_true", help="Skip the cached players.json and re-download")
    args = parser.parse_args()

    print(f"Fetching league {args.league_id}...", file=sys.stderr)
    league = get_league(args.league_id)
    season = league.get("season")
    prev_league_id = league.get("previous_league_id")
    if not prev_league_id or prev_league_id == "0":
        print("ERROR: This league has no previous_league_id -- there's no 'last season' to pull "
              "draft/waiver values from. Point --league-id at the league whose *next* season "
              "you're computing keepers for.", file=sys.stderr)
        sys.exit(1)

    print(f"Current league: {league.get('name')} (season {season})", file=sys.stderr)
    prev_league = get_league(prev_league_id)
    print(f"Previous league: {prev_league.get('name')} (season {prev_league.get('season')})", file=sys.stderr)

    print("Building last-season draft/waiver values...", file=sys.stderr)
    last_season_values = build_last_season_values(prev_league_id)

    print("Loading player metadata...", file=sys.stderr)
    players = get_players(use_cache=not args.no_cache)

    print(f"Loading Sleeper's own draft-room $PROJ values from {args.draft_values}...", file=sys.stderr)
    draft_values = load_draft_values(args.draft_values)

    print("Fetching current rosters/users...", file=sys.stderr)
    rosters = get_rosters(args.league_id)
    users = get_users(args.league_id)
    user_by_id = {u["user_id"]: u for u in users}

    def team_name(roster: dict) -> str:
        owner_id = roster.get("owner_id")
        user = user_by_id.get(owner_id)
        if not user:
            return f"Roster {roster.get('roster_id')}"
        meta = user.get("metadata") or {}
        return meta.get("team_name") or user.get("display_name") or f"Roster {roster.get('roster_id')}"

    rows = []
    for roster in rosters:
        tname = team_name(roster)
        player_ids = roster.get("players") or []
        already_kept = set(roster.get("keepers") or [])
        for pid in player_ids:
            info = players.get(pid) or {}
            name = info.get("full_name") or f"{info.get('first_name','')} {info.get('last_name','')}".strip() or pid
            position = info.get("position") or ""
            nfl_team = info.get("team") or ""

            dv = draft_values.get(pid)
            is_locked_keeper = pid in already_kept
            if dv is None:
                # Players already locked in as *this* year's keeper pick are
                # pulled from the live draft pool entirely, so Sleeper never
                # renders a $PROJ for them -- that's not a missing-data bug,
                # there's just nothing to project. Don't silently show $0.
                proj_pts = 0.0
                proj_dollar_value = 0.0
                note = "ALREADY SET AS THIS YEAR'S KEEPER -- no $PROJ available" if is_locked_keeper else "not found on draft board"
            else:
                proj_pts = dv["points"]
                proj_dollar_value = dv["dollar"]
                note = ""
            last_value = last_season_values.get(pid, 0.0)
            proj_value_share = proj_dollar_value / 3.0
            keeper_value = last_value + proj_value_share

            rows.append({
                "team": tname,
                "player": name,
                "position": position,
                "nfl_team": nfl_team,
                "last_season_cost": round(last_value, 2),
                "projected_points": round(proj_pts, 2),
                "projected_auction_value": round(proj_dollar_value, 2),
                "projected_value_third": round(proj_value_share, 2),
                "keeper_value": round(keeper_value),
                "note": note,
            })

    rows.sort(key=lambda r: (r["team"], -r["keeper_value"]))

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "team", "player", "position", "nfl_team",
            "last_season_cost", "projected_points", "projected_auction_value", "projected_value_third", "keeper_value",
            "note",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {args.out}\n", file=sys.stderr)

    # Console summary: top keeper-value players per team
    max_keepers = (league.get("settings") or {}).get("max_keepers")
    by_team = defaultdict(list)
    for r in rows:
        by_team[r["team"]].append(r)

    for tname, players_list in by_team.items():
        print(f"=== {tname} ===")
        top_n = players_list if not max_keepers else players_list[:max_keepers]
        for r in top_n:
            flag = f"  [{r['note']}]" if r["note"] else ""
            print(f"  {r['player']:<25} {r['position']:<4} "
                  f"last=${r['last_season_cost']:<7} proj/3={r['projected_value_third']:<7} "
                  f"=> keeper_value={r['keeper_value']}{flag}")
        print()


if __name__ == "__main__":
    main()
