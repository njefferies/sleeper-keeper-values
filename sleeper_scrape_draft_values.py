#!/usr/bin/env python3
"""
Scrapes Sleeper's own $PROJ auction-value column straight out of the draft
room UI and writes it to a CSV. This exists because Sleeper does not expose
these dollar values through any public API -- they're computed and pushed to
the draft room client-side/over websocket, so the only reliable way to get
the exact numbers Sleeper itself shows is to drive a real logged-in browser.

One-time setup: run with --login once. It walks you through launching a
completely ordinary (non-automated) Chrome window with a debug port open,
where you log into Sleeper exactly as you always do -- this script never
sees your password. It then attaches to that already-logged-in browser just
to copy out the session cookies/localStorage, saving them to
.sleeper_cache/session_state.json for every later run to reuse headlessly.
No further interaction is needed until the session expires (Sleeper sessions
are long-lived; if it does expire, just run --login again).

(Sleeper's bot-detection flags a browser Playwright launches and controls
from the very start -- even with a real Chrome build and spoofed fingerprint
-- which is why login has to happen in a browser Playwright only attaches to
afterward, rather than one it drives directly.)

Usage:
    python sleeper_scrape_draft_values.py --login --league-id <league_id>
    python sleeper_scrape_draft_values.py --league-id <league_id>
    python sleeper_scrape_draft_values.py --draft-id <draft_id> --out draft_values.csv

Find your league_id in the URL when you open your league on sleeper.com, e.g.
sleeper.com/leagues/<league_id>/team. --draft-id is only needed if you want
to skip the league_id -> draft_id lookup.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

API = "https://api.sleeper.app/v1"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sleeper_cache")
SESSION_STATE_PATH = os.path.join(CACHE_DIR, "session_state.json")

# JS injected into the page: reads the currently-rendered virtualized rows
# out of the draft board and stashes them (deduped by rank) on window.
CAPTURE_JS = """
() => {
  window.__rowMap = window.__rowMap || new Map();
  const grid = document.querySelector('.ReactVirtualized__Grid.ReactVirtualized__List');
  if (!grid) return window.__rowMap.size;
  const rows = grid.querySelectorAll('.player-rank-item2');
  rows.forEach(row => {
    const rankEl = row.querySelector('.rank');
    const nameWrap = row.querySelector('.name-wrapper');
    const adpEl = row.querySelector('.adp .value');
    const ptsEl = row.querySelector('.proj-pts .value');
    if (!rankEl || !nameWrap) return;
    const rank = rankEl.textContent.trim();

    // First text node of .name-wrapper is the player/team name, before the
    // nested .position div.
    let name = '';
    for (const node of nameWrap.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) { name += node.textContent; }
    }
    name = name.trim();

    const posDiv = nameWrap.querySelector('.position');
    let position = null, team = null, injury = null;
    if (posDiv) {
      const teamEl = posDiv.querySelector('.team');
      team = teamEl ? teamEl.textContent.trim() : null;
      const injuryEl = posDiv.querySelector('.injury-status');
      injury = injuryEl ? injuryEl.textContent.trim() : null;
      const clone = posDiv.cloneNode(true);
      clone.querySelectorAll('.team, .injury-status, .position-dot').forEach(e => e.remove());
      position = clone.textContent.trim();
    }

    window.__rowMap.set(rank, {
      rank, name, position, team, injury,
      dollarVal: adpEl ? adpEl.textContent.trim() : null,
      pts: ptsEl ? ptsEl.textContent.trim() : null,
    });
  });
  return window.__rowMap.size;
}
"""

DUMP_JS = "() => JSON.stringify(Array.from((window.__rowMap || new Map()).values()))"


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "keeper-calc/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_draft_id(league_id: str) -> str:
    league = fetch_json(f"{API}/league/{league_id}")
    draft_id = league.get("draft_id")
    if not draft_id:
        raise SystemExit(f"League {league_id} has no draft_id yet.")
    return draft_id


CHROME_DEBUG_PORT = 9222
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chrome_exe():
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def do_login(playwright):
    """
    Sleeper's bot-detection flags *any* Chrome that Playwright launches and
    controls from the start (it can detect the automation/CDP connection
    itself, not just the browser fingerprint) -- so scripted login just
    doesn't work here, captcha or not.

    Instead: you launch a completely ordinary, unautomated Chrome yourself
    (with a debug port open) and log in normally. Only *after* you're logged
    in does this script attach to that already-running Chrome via CDP, purely
    to copy out the session cookies. Nothing about the login itself is
    automated or touched by this script.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    chrome_exe = find_chrome_exe()
    profile_dir = os.path.join(CACHE_DIR, "chrome_login_profile")

    print("\nStep 1: Launch a normal Chrome window with a debug port open, e.g.:\n")
    if chrome_exe:
        print(f'    "{chrome_exe}" --remote-debugging-port={CHROME_DEBUG_PORT} --user-data-dir="{profile_dir}"\n')
    else:
        print(f'    "<path to chrome.exe>" --remote-debugging-port={CHROME_DEBUG_PORT} --user-data-dir="{profile_dir}"\n')
    print("Step 2: In that window, go to sleeper.com and log in exactly as you normally would.")
    print("Step 3: Come back here and press Enter.\n")
    input("Press Enter once you're logged into Sleeper in that Chrome window... ")

    browser = playwright.chromium.connect_over_cdp(f"http://localhost:{CHROME_DEBUG_PORT}")
    if not browser.contexts:
        raise SystemExit("Couldn't find any open browser context on that debug port. Is Chrome running with --remote-debugging-port?")

    print(f"\n[debug] contexts found: {len(browser.contexts)}")
    best_context, best_page = None, None
    for ci, context in enumerate(browser.contexts):
        print(f"[debug] context {ci}: {len(context.pages)} page(s)")
        for page in context.pages:
            print(f"[debug]   page url: {page.url}")
            if "sleeper.com" in page.url:
                best_context, best_page = context, page

    if best_page is None:
        best_context = browser.contexts[0]
        print("[debug] WARNING: no open sleeper.com tab found -- make sure that "
              "tab is still open in the Chrome window before pressing Enter.")
        local_storage_items = []
    else:
        ls = best_page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
        print(f"[debug] localStorage keys on sleeper.com page: {list(ls.keys())}")
        local_storage_items = [{"name": k, "value": v} for k, v in ls.items()]

    # Playwright's context.storage_state() only captures localStorage for pages
    # it navigated itself -- it misses localStorage on a page we merely attached
    # to via CDP after the fact. Cookies work fine through the normal API, but
    # we have to splice the localStorage in manually.
    cookies = best_context.cookies()
    state = {
        "cookies": cookies,
        "origins": [{"origin": "https://sleeper.com", "localStorage": local_storage_items}] if local_storage_items else [],
    }
    with open(SESSION_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)
    browser.close()  # only disconnects; doesn't kill your manually-launched Chrome

    print(f"[debug] saved {len(state['cookies'])} cookies, "
          f"origins with localStorage: {[o['origin'] for o in state['origins']]}")
    print(f"Session saved to {SESSION_STATE_PATH}. Future runs won't need --login.")


def scrape_draft_board(playwright, draft_id: str, headless: bool, max_scrolls: int, scroll_px: int):
    if not os.path.exists(SESSION_STATE_PATH):
        raise SystemExit("No saved session found. Run with --login first.")

    browser = playwright.chromium.launch(
        headless=headless,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=SESSION_STATE_PATH,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = context.new_page()
    page.goto(f"https://sleeper.com/draft/nfl/{draft_id}")

    try:
        page.wait_for_selector(".ReactVirtualized__Grid.ReactVirtualized__List", timeout=15000)
    except PWTimeoutError:
        browser.close()
        raise SystemExit(
            "Couldn't find the draft board -- your saved session may have expired. "
            "Run with --login again."
        )

    # Give the board a moment to populate its first render.
    page.wait_for_timeout(500)

    # "Show Drafted" also reveals players already locked in as someone's
    # keeper for this draft -- without it they're pulled from the pool
    # entirely and never get a $PROJ. Use a real Playwright click (a trusted
    # mouse event); a scripted element.click() via evaluate() does not
    # trigger React's handler here.
    try:
        page.click(".drafted-filter.filter-button", timeout=5000)
        page.wait_for_timeout(300)
    except PWTimeoutError:
        print("[warn] couldn't find/click the 'Show Drafted' toggle -- "
              "already-kept players may be missing $PROJ values.", file=sys.stderr)

    grid_box = page.query_selector(".ReactVirtualized__Grid.ReactVirtualized__List").bounding_box()
    center_x = grid_box["x"] + grid_box["width"] / 2
    center_y = grid_box["y"] + grid_box["height"] / 2
    page.mouse.move(center_x, center_y)

    # Capture the initial (unscrolled) screenful first -- otherwise the
    # top-ranked players (rank 1, 2, 3...) never get captured, since the loop
    # below only captures *after* each scroll step.
    prev_count = page.evaluate(CAPTURE_JS)
    stable_rounds = 0
    for i in range(max_scrolls):
        page.mouse.wheel(0, scroll_px)
        page.wait_for_timeout(120)
        count = page.evaluate(CAPTURE_JS)
        if count == prev_count:
            stable_rounds += 1
            if stable_rounds >= 5:
                break  # reached the bottom of the list
        else:
            stable_rounds = 0
        prev_count = count

    raw = page.evaluate(DUMP_JS)
    browser.close()
    return json.loads(raw)


def normalize_dollar(v):
    if not v:
        return None
    v = v.replace("$", "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def normalize_pts(v):
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_players_index(no_cache: bool = False):
    """player full name (lowercased, alnum-only) + position -> player_id, for joining."""
    path = os.path.join(CACHE_DIR, "players.json")
    if not no_cache and os.path.exists(path) and (time.time() - os.path.getmtime(path)) < 7 * 24 * 3600:
        with open(path, "r", encoding="utf-8") as f:
            players = json.load(f)
    else:
        print("Downloading full Sleeper player list...", file=sys.stderr)
        players = fetch_json(f"{API}/players/nfl")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(players, f)

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    index = {}
    name_only_index = defaultdict(list)
    for pid, info in players.items():
        if not isinstance(info, dict):
            continue
        full_name = info.get("full_name") or f"{info.get('first_name','')} {info.get('last_name','')}"
        pos = info.get("position")
        key = (norm(full_name), pos)
        index[key] = pid
        name_only_index[norm(full_name)].append(pid)
        # DEF entries: full_name may be blank; use team city+name from first/last
        if pos == "DEF":
            def_name = f"{info.get('first_name','')}{info.get('last_name','')}"
            index[(norm(def_name), "DEF")] = pid
            name_only_index[norm(def_name)].append(pid)
    return index, name_only_index, norm


def main():
    parser = argparse.ArgumentParser(description="Scrape Sleeper's draft-room $PROJ auction values into a CSV.")
    parser.add_argument("--league-id", help="Sleeper league_id (used to look up the draft_id)")
    parser.add_argument("--draft-id", help="Sleeper draft_id directly (skips the league lookup)")
    parser.add_argument("--out", default="draft_values.csv", help="Output CSV path")
    parser.add_argument("--login", action="store_true", help="Run one-time interactive login and save the session")
    parser.add_argument("--headless", action="store_true", default=True, help="Run headless (default)")
    parser.add_argument("--headed", action="store_false", dest="headless", help="Show the browser while scraping")
    parser.add_argument("--max-scrolls", type=int, default=120, help="Safety cap on scroll iterations")
    parser.add_argument("--scroll-px", type=int, default=500, help="Wheel-scroll distance per step")
    parser.add_argument("--no-cache", action="store_true", help="Skip cached players.json and re-download")
    args = parser.parse_args()

    with sync_playwright() as p:
        if args.login:
            do_login(p)
            if not args.league_id and not args.draft_id:
                return

        if args.draft_id:
            draft_id = args.draft_id
        elif args.league_id:
            draft_id = resolve_draft_id(args.league_id)
        else:
            raise SystemExit("Provide --league-id or --draft-id (or just --login to set up the session).")

        print(f"Scraping draft board {draft_id}...", file=sys.stderr)
        rows = scrape_draft_board(p, draft_id, args.headless, args.max_scrolls, args.scroll_px)

    print(f"Captured {len(rows)} rows from the draft board.", file=sys.stderr)

    players_index, name_only_index, norm = load_players_index(no_cache=args.no_cache)

    out_rows = []
    unmatched = 0
    for r in rows:
        dollar = normalize_dollar(r.get("dollarVal"))
        pts = normalize_pts(r.get("pts"))
        name = r.get("name") or ""
        pos = r.get("position")
        team = r.get("team")
        pid = players_index.get((norm(name), pos))
        if pid is None and pos:
            # Multi-eligibility players show comma-joined positions on the
            # board (e.g. "DB,WR") that won't match players.json's single
            # position -- try each one.
            for p in pos.split(","):
                pid = players_index.get((norm(name), p.strip()))
                if pid:
                    break
        if pid is None:
            # Last resort: unique name-only match.
            candidates = name_only_index.get(norm(name)) or []
            if len(candidates) == 1:
                pid = candidates[0]
        if pid is None:
            unmatched += 1
        out_rows.append({
            "player_id": pid or "",
            "name": name,
            "position": pos or "",
            "team": team or "",
            "draft_dollar_value": dollar if dollar is not None else "",
            "draft_projected_points": pts if pts is not None else "",
        })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "player_id", "name", "position", "team", "draft_dollar_value", "draft_projected_points",
        ])
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Wrote {len(out_rows)} rows to {args.out} ({unmatched} unmatched to a player_id).", file=sys.stderr)


if __name__ == "__main__":
    main()
