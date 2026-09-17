# MLB Live Score

MLB Live Score is a lightweight Pillow-rendered scoreboard backed directly by MLB's public StatsAPI. It requires no API key.

Select any of the 30 MLB clubs in the InkyPi settings page. The plugin prioritizes a live game, then a completed game today, then an upcoming game today, and finally the next scheduled game. Doubleheaders follow the same deterministic ordering.

Live screens show away/home R/H/E, inning half, outs, pitcher, batter, balls-strikes count, and official base occupancy. Pregame screens show scheduled time, venue, and probable starters. Final screens show the winning and losing pitchers with their postgame season records, plus the save pitcher and season save total when MLB publishes a save. When the team is idle, the next matchup and probable pitchers are shown. Every non-live screen uses the right panel for the selected team's official division standings.

MLB may temporarily omit hits, errors, probable pitchers, current matchup data, decisions, or runners. Missing fields render as dashes or `TBD`. A schedule or live-feed failure raises a clear plugin error. A standings-only failure does not prevent game rendering: a cached copy is used and marked `CACHED`, or the panel says `STANDINGS UNAVAILABLE`. Standings are cached for 20 minutes; live game data is fetched on every refresh and is never served from that cache.

For an e-paper display, use a 5–10 minute refresh during game windows, or a slower interval outside games. MLB StatsAPI timing and field availability are controlled by MLB.

## Development previews

Generate every representative offline preview from the repository root:

```bash
PYTHONPATH=src python src/plugins/mlb_live_score/preview.py
```

PNGs are written to `mock_display_output/mlb_live_score/`. These fixtures never contact MLB. The standard development UI uses real API data:

```bash
python src/inkypi.py --dev
```

Open `http://localhost:8080`, choose **MLB Live Score**, select a team, and click **Display**.

## Dynamic Scheduler

The scheduler state contract exposes the selected game's lifecycle, inning,
scores, absolute score difference, lead/trail/tie state, scheduled start, and
game-today flag. A game is considered close only while live and within two
runs. The metadata contract supplies friendly fields to the visual rule
builder. Scheduler evaluation and an immediate render share the plugin's
30-second normalized presentation cache.

Two boolean fields support nearby pregame/final displays:
`game.pregame_within_2_hours` requires today's unstarted game and scheduled first
pitch zero to two hours ahead. Next-day games and overdue delayed starts do not
qualify. `game.final_within_3_hours` requires today's final and an end timestamp
zero to three hours old, on the current device-local day. The timestamp is the
existing live feed's completed terminal play `about.endTime`, not its publication
time or when this plugin first saw Final. This is a conservative proxy: called
games or administrative decisions may become final after the last baseball
action, causing the flag to expire early. Missing/incomplete/naive timestamps
return false; no extra request or estimated game length is used.

Half-inning transitions are normalized atomically. A new current play with its
own inning, matchup and count, or a verified next batting team with both changed
participants, can establish the new half. Otherwise the ended play's half,
participants, count, bases and available scores remain together. Unverifiable
participants/counts are shown as unavailable rather than guessed. Automatic
extra-inning runners are preserved when the next offense is identified.

The live diamond heading describes the same occupancy used to draw its bases,
using canonical ALL CAPS labels centered over the diamond.
Probable and decision pitchers share the existing initial/surname presentation,
with their optional record/save total immediately beside the name in parentheses.
