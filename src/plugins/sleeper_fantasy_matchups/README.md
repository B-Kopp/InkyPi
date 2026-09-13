# Sleeper Fantasy Matchups

A native Pillow dashboard showing the current matchup in one to four Sleeper NFL leagues.

## Setup

Enter a Sleeper username or numerical user ID, click **Find current leagues**, and choose one to four current-season leagues in display order. League IDs may also be entered manually. Sleeper's read-only public API requires no API key.

Each league is resolved independently: the plugin finds the selected user's `roster_id` from `owner_id`, then pairs that roster with the other entry having the same weekly `matchup_id`. Actual scores always come from the matchup endpoint's league-scored `points`; a non-null commissioner `custom_points` override takes precedence. Team records and scoring settings are also league-specific. A failed league becomes an unavailable card without suppressing successful cards.

## Data and estimates

Supported public Sleeper endpoints are authoritative for NFL state, users, leagues, league metadata, rosters, league users, matchups, and actual scores. See [Sleeper's API documentation](https://docs.sleeper.com/). Public endpoints use short resource-appropriate in-memory caches: NFL state and discovery 10 minutes, league metadata 1 hour, league users/rosters 30 minutes, and `(league_id, week)` matchups 90 seconds. Player metadata, if needed in a future context display, is assigned a 24-hour policy.

Projected player statistics and NFL game status come from Sleeper's undocumented `api.sleeper.com` projection/schedule services. They are isolated behind an optional adapter and are not guaranteed. Schedule rows with a blank status are treated as valid scheduled games. If either service is unavailable or a starter cannot be evaluated reliably, projected totals, remaining-player context, and win chance are omitted while current matchup scores remain visible. The schedule feed does not always expose live clock progress; in that case the estimate is omitted rather than assigning a guessed remaining fraction.

Matchup state is derived from the selected starters' NFL games. Any live game makes the matchup live; a mixture of completed and later scheduled games is also live; and all completed relevant games make it final. Non-zero matchup or player scoring is a fallback signal that kickoff occurred when optional schedule data is stale or incomplete, so a scoring matchup is never labeled pregame.

Projected final score is current league-scored points plus only expected points remaining. A starter whose game has not begun contributes the full projection, a completed starter contributes zero, and a live starter contributes the portion corresponding to estimated regulation time remaining. Raw projections are converted with that league's own `scoring_settings`.

Sleeper does **not** supply matchup win probability. **EST. WIN CHANCE** is calculated locally. Remaining-player variance uses position-based coefficients of variation (QB .38, RB .58, WR .64, TE .66, K .55, DEF .52, with conservative IDP/default values). Player variances are aggregated under an independence approximation, and the expected final margin is evaluated with a Normal CDF. Live probabilities are capped to 1%–99%, final results are 100%/0%, and final ties display as ties. The estimate is omitted when remaining projections or uncertainty are unreliable.

For active NFL games, use a refresh interval of approximately **2–5 minutes**.

## Offline previews

From the repository root:

```bash
PYTHONPATH=src .venv/bin/python -m plugins.sleeper_fantasy_matchups.preview
```

This writes all one-, two-, three-, and four-card fixture variants to `mock_display_output/sleeper_fantasy_matchups/` without contacting Sleeper.

Useful visual-review files include:

- `one_close_live.png`, `two_live.png`, `three_mixed.png`, and `four_live.png` for each density mode
- `long_names_high_scores.png`, `two_long_names.png`, `three_long_names.png`, and `four_long_names.png` for fitting and truncation
- `projections_unavailable.png` and `four_projections_unavailable.png` for unavailable probability/projection states
- `two_live_final.png`, `four_mixed.png`, `one_no_matchup.png`, and `four_one_failed.png` for mixed live/final/pregame/bye/no-matchup/error states

For an exact density review at 800×480, open these generated files:

- 1 league / HERO: `mock_display_output/sleeper_fantasy_matchups/one_close_live.png`
- 2 leagues / LARGE: `mock_display_output/sleeper_fantasy_matchups/two_live.png`
- 3 leagues / MEDIUM: `mock_display_output/sleeper_fantasy_matchups/three_mixed.png`
- 4 leagues / COMPACT: `mock_display_output/sleeper_fantasy_matchups/four_live.png`

For the live browser preview, run `.venv/bin/python src/inkypi.py --dev`, open `http://127.0.0.1:8080/plugin/sleeper_fantasy_matchups`, select one to four leagues, and use **Update Now**. The home page at `http://127.0.0.1:8080/` refreshes the rendered display automatically.
