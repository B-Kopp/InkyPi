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
