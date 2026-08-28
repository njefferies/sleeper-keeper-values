# sleeper-keeper-values

Calculates fantasy football keeper values for a [Sleeper](https://sleeper.com)
auction-draft league, using this formula:

```
if last_season_cost > this_season_projected_value:
    keeper_value = last_season_cost
else:
    keeper_value = min(
        last_season_cost + (this_season_projected_value - last_season_cost) / 3,
        this_season_projected_value,
    )
```

- **`last_season_cost`** -- the real $ cost a player was acquired for last
  season: their winning auction bid, or (if picked up off waivers/free
  agency at any point) the FAAB dollars spent on the most recent successful
  add, whichever happened most recently. Trades don't reset this -- it
  always reflects how the player originally entered the league that season.
  Brand new players default to $0.
- **`this_season_projected_value`** -- Sleeper's own `$PROJ` auction-value
  column from the draft room, i.e. what Sleeper itself projects the player
  would go for in your league's auction this year. Not a home-grown estimate
  -- Sleeper doesn't expose this number through any public API, so it's
  scraped directly out of the (authenticated) draft room UI.

In plain terms: if last season's cost already exceeds this year's market
value, keeping the player is already a bad-enough deal -- leave the cost as
is. Otherwise, nudge the cost up by 1/3 of the gap toward market value (a
"keeper tax" on holding a bargain), capped so it never exceeds market value
itself. A free agent (`last_season_cost = 0`) simply works out to 1/3 of
their projected auction value.

Adjust the formula in [`sleeper_keeper_values.py`](sleeper_keeper_values.py)
if your league's rules differ.

## How it works

Two scripts:

1. **`sleeper_scrape_draft_values.py`** -- logs into Sleeper and scrapes the
   `$PROJ` column off your league's draft board (including players already
   locked in as someone's keeper pick, via the "Show Drafted" toggle) into
   `draft_values.csv`. There's no public API for this number -- it's
   rendered client-side in the draft room -- so getting the exact figure
   Sleeper shows means driving a real, logged-in browser.
2. **`sleeper_keeper_values.py`** -- pulls your league's rosters, last
   season's auction draft, and every waiver/FAAB transaction from Sleeper's
   public API, joins in `draft_values.csv`, and writes `keeper_values.csv`
   with every rostered player's keeper value.

### Why a browser at all?

Sleeper's bot-detection flags a Chrome instance that Playwright launches and
controls from the start -- real Chrome build, spoofed fingerprint, none of
it matters, it still gets challenged. The workaround: you log in through a
completely ordinary, manually-launched Chrome window (not automated), and
the script only attaches to it *afterward*, purely to copy out the session
cookies. Nothing about the login itself is scripted.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

You'll also need your league's `league_id`, found in the URL when you open
your league on sleeper.com (e.g. `sleeper.com/leagues/<league_id>/team`).

## Usage

**One-time login** (repeat only if your saved session expires -- Sleeper
sessions are long-lived, so this is usually a once-a-year thing):

```bash
python sleeper_scrape_draft_values.py --login
```

This prints a command to launch a plain Chrome window with a debug port
open. Run it, log into Sleeper in that window exactly as you normally would,
then come back and press Enter. Your session is saved to
`.sleeper_cache/session_state.json` -- **treat this file like a password,
never commit or share it** (it's already gitignored).

**Every other time** (fully unattended, headless):

```bash
python sleeper_scrape_draft_values.py --league-id <league_id> --out draft_values.csv
python sleeper_keeper_values.py --league-id <league_id> --draft-values draft_values.csv
```

`keeper_values.csv` comes out with one row per rostered player:

| column | meaning |
|---|---|
| `team`, `player`, `position`, `nfl_team` | who and where |
| `last_season_cost` | what they cost you last season (draft bid or FAAB) |
| `projected_points` | Sleeper's projected season-long fantasy points |
| `projected_auction_value` | Sleeper's own `$PROJ` -- what they'd go for in this year's auction |
| `keeper_value` | what keeping them actually costs you, per the formula above |
| `keeper_tax` | `keeper_value - last_season_cost` -- how much the formula bumped the cost up from what you paid last year (0 if last year's cost already exceeded market value) |
| `keeper_savings` | `projected_auction_value - keeper_value` -- how much cheaper keeping them is than drafting them fresh this year (negative means keeping them is actually a worse deal) |

It also prints each team's top keepers (up to your league's `max_keepers`
setting) to the console.

### Options

`sleeper_scrape_draft_values.py`:

| flag | default | description |
|---|---|---|
| `--league-id` | -- | Sleeper league_id (looks up the draft_id for you) |
| `--draft-id` | -- | Sleeper draft_id directly, skips the league lookup |
| `--out` | `draft_values.csv` | output CSV path |
| `--login` | off | run one-time interactive login and save the session |
| `--headed` | off | show the browser while scraping (default is headless) |
| `--no-cache` | off | force re-download of the player list |

`sleeper_keeper_values.py`:

| flag | default | description |
|---|---|---|
| `--league-id` | *(required)* | current season's Sleeper league_id |
| `--draft-values` | `draft_values.csv` | CSV from the scraper |
| `--out` | `keeper_values.csv` | output CSV path |
| `--no-cache` | off | force re-download of the player list |

## Notes & caveats

- Only tested against **auction** leagues with **FAAB waivers**. A snake/linear
  draft league would need `last_season_cost` reworked to use draft-pick
  value instead of auction dollars.
- Players already set as *this* year's keeper by their owner get pulled from
  the live draft pool, but the scraper turns on Sleeper's "Show Drafted"
  toggle specifically so they still get a `$PROJ` value.
- A player with no draft/waiver history in the previous season's league
  (e.g. added by trade, or the league is brand new) defaults to a $0 last-
  season cost.
- Both scripts cache the full Sleeper player list (`players.json`, a few MB)
  in `.sleeper_cache/` for a week to avoid re-downloading it every run.

## Requirements

- Python 3.8+
- Google Chrome installed (the scraper drives your real Chrome, not a
  bundled browser)
- [Playwright](https://playwright.dev/python/) (`sleeper_keeper_values.py`
  itself only uses the standard library)
