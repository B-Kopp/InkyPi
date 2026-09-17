"""MLB StatsAPI access and response normalization."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from utils.http_client import get_http_session

from .models import (
    DivisionStandingRow,
    DivisionStandings,
    GameSummary,
    ScoreboardPresentation,
    classify_game_state,
)

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api"
REQUEST_TIMEOUT = (5, 15)
STANDINGS_CACHE_TTL = timedelta(minutes=20)

TEAM_ABBREVIATIONS = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC", 119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD", 136: "SEA", 137: "SF", 138: "STL",
    139: "TB", 140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def _dig(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def _game_time(game: dict[str, Any]) -> datetime:
    parsed = _parse_datetime(game.get("gameDate"))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def _official_date(game: dict[str, Any]) -> str:
    return str(game.get("officialDate") or game.get("gameDate") or "")[:10]


def select_relevant_game(
    games: list[dict[str, Any]], today: date
) -> tuple[dict[str, Any] | None, bool]:
    """Apply the deterministic live/today/future selection policy."""
    live = [g for g in games if classify_game_state(g.get("status")).uses_live_layout]
    if live:
        return sorted(live, key=_game_time)[0], _official_date(live[0]) != today.isoformat()

    todays_games = [g for g in games if _official_date(g) == today.isoformat()]
    if todays_games:
        finals = [g for g in todays_games if classify_game_state(g.get("status")).is_final]
        if finals:
            return sorted(finals, key=_game_time, reverse=True)[0], False
        upcoming = [
            g for g in todays_games
            if classify_game_state(g.get("status")).state.value not in {"cancelled", "postponed"}
        ]
        if upcoming:
            return sorted(upcoming, key=_game_time)[0], False
        return sorted(todays_games, key=_game_time)[0], False

    future = [
        g for g in games
        if _official_date(g) > today.isoformat()
        and classify_game_state(g.get("status")).state.value not in {"cancelled", "postponed", "final"}
    ]
    if future:
        return sorted(future, key=_game_time)[0], True
    return None, True


class MlbDataClient:
    """Small data-access layer for MLB schedule, live feed and standings."""

    def __init__(self, session=None, now_provider=None):
        self.session = session or get_http_session()
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())
        self._standings_cache: dict[tuple[int, int], tuple[datetime, DivisionStandings]] = {}
        self._team_division_cache: dict[tuple[int, int], tuple[int, str, int]] = {}

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{MLB_API}{path}", params=params or {}, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response was not a JSON object")
            return payload
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("MLB StatsAPI request failed for %s: %s", path, exc)
            raise RuntimeError("MLB StatsAPI is temporarily unavailable.") from exc

    def discover_games(self, team_id: int, today: date) -> list[dict[str, Any]]:
        data = self._request_json(
            "/v1/schedule",
            {
                "sportId": 1,
                "teamId": team_id,
                # Include recent dates so a delayed/suspended contest that crossed
                # midnight can still win the live-game selection rule.
                "startDate": (today - timedelta(days=7)).isoformat(),
                "endDate": (today + timedelta(days=30)).isoformat(),
                "hydrate": "team,probablePitcher,linescore",
            },
        )
        return [
            game
            for day_data in data.get("dates", []) or []
            for game in (day_data.get("games", []) or [])
            if isinstance(game, dict)
        ]

    def get_presentation(self, team_id: int) -> ScoreboardPresentation:
        if team_id not in TEAM_ABBREVIATIONS:
            raise RuntimeError("Please select a valid MLB team.")
        now = self.now_provider()
        games = self.discover_games(team_id, now.date())
        selected, no_game_today = select_relevant_game(games, now.date())
        if selected is None:
            team_name = TEAM_ABBREVIATIONS[team_id]
            return ScoreboardPresentation(
                team_id, team_name, team_name, None, no_game_today=True, updated_at=now
            )

        game_pk = _int_or_none(selected.get("gamePk"))
        if game_pk is None:
            raise RuntimeError("MLB returned a game without an identifier.")
        feed = self._request_json(f"/v1.1/game/{game_pk}/feed/live")
        game = normalize_game(feed, selected)
        if game.is_pregame:
            self._fill_missing_starter_records(game, feed, selected)

        selected_side = "away" if game.away_team_id == team_id else "home"
        selected_team = _dig(feed, "gameData", "teams", selected_side, default={})
        selected_name = selected_team.get("name") or TEAM_ABBREVIATIONS[team_id]
        selected_abbr = team_abbreviation(selected_team, team_id)
        presentation = ScoreboardPresentation(
            selected_team_id=team_id,
            selected_team_name=selected_name,
            selected_team_abbreviation=selected_abbr,
            game=game,
            no_game_today=no_game_today,
            updated_at=now,
        )
        if not game.uses_live_layout:
            try:
                presentation.standings = self.get_division_standings(
                    team_id, game.season or now.year
                )
            except RuntimeError:
                cached = self._cached_standings_for_team(
                    team_id, game.season or now.year, allow_stale=True
                )
                if cached:
                    cached.is_stale = True
                    presentation.standings = cached
                else:
                    presentation.standings_unavailable = True
        return presentation

    def _fill_missing_starter_records(
        self, game: GameSummary, feed: dict[str, Any], schedule: dict[str, Any]
    ) -> None:
        """Use one batched player request when current game payloads lack a record."""
        missing: dict[int, str] = {}
        for side in ("away", "home"):
            name = getattr(game, f"{side}_starting_pitcher")
            wins = getattr(game, f"{side}_starting_pitcher_wins")
            losses = getattr(game, f"{side}_starting_pitcher_losses")
            pitcher = _probable_pitcher(feed, schedule, side)
            pitcher_id = _int_or_none(pitcher.get("id"))
            if name and (wins is None or losses is None) and pitcher_id is not None:
                missing[pitcher_id] = side
        if not missing:
            return

        try:
            data = self._request_json(
                "/v1/people",
                {
                    "personIds": ",".join(str(player_id) for player_id in missing),
                    "hydrate": (
                        "stats(group=[pitching],type=[season],"
                        f"season={game.season or self.now_provider().year})"
                    ),
                },
            )
        except RuntimeError:
            return

        for person in data.get("people", []) or []:
            side = missing.get(_int_or_none(person.get("id")))
            if side is None:
                continue
            pitching = _season_pitching_stats(person)
            wins = _int_or_none(pitching.get("wins"))
            losses = _int_or_none(pitching.get("losses"))
            if wins is not None and losses is not None:
                setattr(game, f"{side}_starting_pitcher_wins", wins)
                setattr(game, f"{side}_starting_pitcher_losses", losses)

    def _cached_standings(
        self, division_id: int, season: int, team_id: int, allow_stale: bool = False
    ) -> DivisionStandings | None:
        cached = self._standings_cache.get((season, division_id))
        if not cached:
            return None
        cached_at, value = cached
        if not allow_stale and self.now_provider() - cached_at > STANDINGS_CACHE_TTL:
            return None
        result = deepcopy(value)
        for row in result.rows:
            row.is_selected_team = row.abbreviation == TEAM_ABBREVIATIONS.get(team_id)
        return result

    def _cached_standings_for_team(
        self, team_id: int, season: int, allow_stale: bool = False
    ) -> DivisionStandings | None:
        division = self._team_division_cache.get((team_id, season))
        if division is None:
            return None
        division_id, _, _ = division
        return self._cached_standings(division_id, season, team_id, allow_stale)

    def _division_for_team(self, team_id: int, season: int) -> tuple[int, str, int]:
        cache_key = (team_id, season)
        cached = self._team_division_cache.get(cache_key)
        if cached is not None:
            return cached

        team_data = self._request_json(
            f"/v1/teams/{team_id}", {"hydrate": "division", "season": season}
        )
        teams = team_data.get("teams") or []
        team = teams[0] if teams and isinstance(teams[0], dict) else {}
        division = team.get("division") or {}
        division_id = _int_or_none(division.get("id"))
        league_id = _int_or_none(_dig(team, "league", "id"))
        if division_id is None or league_id is None:
            raise RuntimeError("MLB division information is unavailable.")
        division_name = str(division.get("nameShort") or division.get("name") or "").strip()
        if not division_name:
            raise RuntimeError("MLB division information is unavailable.")
        result = (division_id, division_name, league_id)
        self._team_division_cache[cache_key] = result
        return result

    def get_division_standings(self, team_id: int, season: int) -> DivisionStandings:
        division_id, division_name, league_id = self._division_for_team(team_id, season)
        cached = self._cached_standings(division_id, season, team_id)
        if cached:
            return cached
        data = self._request_json(
            "/v1/standings",
            {
                "leagueId": league_id,
                "divisionId": division_id,
                "season": season,
                "standingsTypes": "regularSeason",
                "hydrate": "team",
            },
        )
        records = data.get("records") or []
        division_record = next(
            (
                record for record in records
                if isinstance(record, dict)
                and _int_or_none(_dig(record, "division", "id")) == division_id
            ),
            None,
        )
        if division_record is None:
            raise RuntimeError("MLB standings are unavailable for the selected division.")
        team_records = division_record.get("teamRecords") or []
        rows = []
        for record in team_records:
            record_team = record.get("team") or {}
            record_id = _int_or_none(record_team.get("id"))
            gb = record.get("gamesBack")
            rows.append(
                DivisionStandingRow(
                    abbreviation=team_abbreviation(record_team, record_id),
                    wins=_int_or_none(record.get("wins")) or 0,
                    losses=_int_or_none(record.get("losses")) or 0,
                    games_back="-" if str(gb or "-") in {"-", "0", "0.0"} else str(gb),
                    is_selected_team=record_id == team_id,
                )
            )
        if not rows:
            raise RuntimeError("MLB standings are unavailable.")
        result = DivisionStandings(division_name.upper(), rows)
        self._standings_cache[(season, division_id)] = (self.now_provider(), deepcopy(result))
        return result


def team_abbreviation(team: dict[str, Any], team_id: int | None) -> str:
    return str(team.get("abbreviation") or TEAM_ABBREVIATIONS.get(team_id, "---")).upper()


def _team_values(feed: dict[str, Any], schedule: dict[str, Any], side: str) -> tuple:
    team = _dig(feed, "gameData", "teams", side, default={})
    scheduled = _dig(schedule, "teams", side, "team", default={})
    team_id = _int_or_none(team.get("id") or scheduled.get("id"))
    name = str(team.get("name") or scheduled.get("name") or side.title())
    abbreviation = team_abbreviation(team or scheduled, team_id)
    box_stats = _dig(feed, "liveData", "boxscore", "teams", side, "teamStats", "batting", default={})
    line = _dig(feed, "liveData", "linescore", "teams", side, default={})
    scheduled_side = _dig(schedule, "teams", side, default={})
    runs = _int_or_none(box_stats.get("runs"))
    if runs is None:
        runs = _int_or_none(line.get("runs"))
    if runs is None:
        runs = _int_or_none(scheduled_side.get("score"))
    hits = _int_or_none(box_stats.get("hits"))
    if hits is None:
        hits = _int_or_none(line.get("hits"))
    errors = _int_or_none(box_stats.get("errors"))
    if errors is None:
        errors = _int_or_none(line.get("errors"))
    probable = _probable_pitcher(feed, schedule, side)
    probable_stats = _pitching_stats_for_player(feed.get("liveData") or {}, probable)
    if not probable_stats:
        probable_stats = _season_pitching_stats(probable)
    return (
        team_id, name, abbreviation, runs, hits, errors,
        probable.get("fullName"),
        _int_or_none(probable_stats.get("wins")),
        _int_or_none(probable_stats.get("losses")),
    )


def _probable_pitcher(
    feed: dict[str, Any], schedule: dict[str, Any], side: str
) -> dict[str, Any]:
    scheduled = _dig(schedule, "teams", side, "probablePitcher", default={})
    live = _dig(feed, "gameData", "probablePitchers", side, default={})
    scheduled = scheduled if isinstance(scheduled, dict) else {}
    live = live if isinstance(live, dict) else {}
    return {**scheduled, **live}


def _season_pitching_stats(player: dict[str, Any] | None) -> dict[str, Any]:
    """Read an official season pitching line from a hydrated MLB person."""
    player = player or {}
    season_stats = _dig(player, "seasonStats", "pitching", default={})
    if season_stats:
        return season_stats
    for stat_group in player.get("stats", []) or []:
        if _dig(stat_group, "group", "displayName") != "pitching":
            continue
        for split in stat_group.get("splits", []) or []:
            stat = split.get("stat") or {}
            if stat:
                return stat
    return {}


def _pitching_stats_for_player(
    live_data: dict[str, Any], player: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a player's official season line from the current boxscore."""
    player = player or {}
    player_id = _int_or_none(player.get("id"))
    player_name = str(player.get("fullName") or "")
    boxscore_teams = _dig(live_data, "boxscore", "teams", default={})
    for side in ("away", "home"):
        players = _dig(boxscore_teams, side, "players", default={})
        candidates = []
        if player_id is not None:
            candidates.append(players.get(f"ID{player_id}") or {})
        if not candidates or not candidates[0]:
            candidates.extend(
                value for value in players.values()
                if player_name and _dig(value, "person", "fullName") == player_name
            )
        for candidate in candidates:
            pitching = _season_pitching_stats(candidate)
            if pitching:
                return pitching
    return {}


def _decision_pitching_stats(
    live_data: dict[str, Any], decision: dict[str, Any] | None
) -> dict[str, Any]:
    """Return the official decision pitcher's postgame season pitching line."""
    pitcher_id = _int_or_none((decision or {}).get("id"))
    if pitcher_id is None:
        return {}
    return _pitching_stats_for_player(live_data, decision)


def _terminal_play_end(live_data):
    """Conservative final-time proxy from the existing feed; never estimate.

    A normal final's last completed play is when baseball action ended. Called
    games/administrative finals can be declared later, so this proxy may expire
    early. Feed metadata timestamps describe publication, not game completion.
    Missing/incomplete/naive terminal timestamps deliberately remain unavailable.
    """
    plays = live_data.get("plays") or {}
    all_plays = plays.get("allPlays") or []
    terminal = all_plays[-1] if all_plays else plays.get("currentPlay") or {}
    about = terminal.get("about") or {}
    return _parse_datetime(about.get("endTime")) if about.get("isComplete") is True else None


def _live_snapshot(linescore, current_play, away_id, home_id, all_plays=()):
    """Keep half-inning transitions atomic rather than mixing feed sections."""
    offense, defense = linescore.get("offense") or {}, linescore.get("defense") or {}
    matchup, count = current_play.get("matchup") or {}, current_play.get("count") or {}
    score_result = current_play.get("result") or {}
    about = current_play.get("about") or {}
    inning = _int_or_none(linescore.get("currentInning"))
    half = str(linescore.get("inningHalf") or "").upper()
    if half not in {"TOP", "BOTTOM"} and isinstance(linescore.get("isTopInning"), bool):
        half = "TOP" if linescore["isTopInning"] else "BOTTOM"
    outs = _int_or_none(linescore.get("outs"))
    if outs is None:
        outs = _int_or_none(count.get("outs"))
    snapshot = dict(inning=inning, inning_half=half or None, outs=outs,
                    balls=_int_or_none(count.get("balls")), strikes=_int_or_none(count.get("strikes")),
                    pitcher_name=_dig(defense, "pitcher", "fullName") or _dig(matchup, "pitcher", "fullName"),
                    batter_name=_dig(offense, "batter", "fullName") or _dig(matchup, "batter", "fullName"),
                    runner_on_first=bool(offense.get("first")), runner_on_second=bool(offense.get("second")),
                    runner_on_third=bool(offense.get("third")))
    play_inning = _int_or_none(about.get("inning"))
    play_half = ("TOP" if about["isTopInning"] else "BOTTOM") if isinstance(about.get("isTopInning"), bool) else None
    play_outs = _int_or_none(count.get("outs"))
    mismatch = play_inning and play_half and (play_inning, play_half) != (inning, half)
    if outs != 3 and play_outs != 3 and not mismatch:
        return snapshot

    # A new currentPlay with its own half, participants and count is stronger
    # evidence than a lagging linescore showing the previous half's third out.
    pair = (_dig(matchup, "pitcher", "fullName"), _dig(matchup, "batter", "fullName"))
    completed = [play for play in all_plays if _dig(play, "about", "isComplete") is True]
    previous = completed[-1] if completed else {}
    previous_inning = _int_or_none(_dig(previous, "about", "inning"))
    previous_top = _dig(previous, "about", "isTopInning")
    proven_next = (previous_inning is not None and isinstance(previous_top, bool)
                   and _dig(previous, "count", "outs") == 3
                   and (play_inning, play_half) ==
                   (previous_inning + (not previous_top), "BOTTOM" if previous_top else "TOP"))
    next_from_line = (outs == 3 and inning is not None and half in {"TOP", "BOTTOM"}
                      and (play_inning, play_half) ==
                      (inning + (half == "BOTTOM"), "BOTTOM" if half == "TOP" else "TOP"))
    if play_inning and play_half and play_outs in {0, 1, 2} and all(pair) and (next_from_line or proven_next):
        snapshot.update(inning=play_inning, inning_half=play_half, outs=play_outs,
                        pitcher_name=pair[0], batter_name=pair[1])
        if mismatch or outs == 3:
            # Never carry the previous offense's runners into a new half.
            batting_team = away_id if play_half == "TOP" else home_id
            aligned_offense = (batting_team is not None and _dig(offense, "team", "id") == batting_team
                               and _dig(offense, "batter", "fullName") == pair[1])
            for base, field in (("First", "first"), ("Second", "second"), ("Third", "third")):
                snapshot[f"runner_on_{field}"] = bool(offense.get(field) if aligned_offense else matchup.get(f"postOn{base}"))
        return snapshot

    if outs == 3 and play_outs != 3:
        # Same-half incomplete currentPlay cannot justify resetting a third out.
        # Recover the ended play when available; otherwise hide unverified live
        # details rather than present a guessed mix of participants/count/bases.
        if _dig(previous, "count", "outs") != 3:
            snapshot.update(pitcher_name=None, batter_name=None, balls=None, strikes=None,
                            runner_on_first=False, runner_on_second=False, runner_on_third=False)
            return snapshot
        about, matchup, count = (previous.get("about") or {}, previous.get("matchup") or {}, previous.get("count") or {})
        play_inning = _int_or_none(about.get("inning"))
        play_half = ("TOP" if about["isTopInning"] else "BOTTOM") if isinstance(about.get("isTopInning"), bool) else None
        play_outs = 3
        pair = (_dig(matchup, "pitcher", "fullName"), _dig(matchup, "batter", "fullName"))
        score_result = previous.get("result") or {}
        snapshot.update(balls=_int_or_none(count.get("balls")), strikes=_int_or_none(count.get("strikes")))

    ended_inning, ended_half = play_inning or inning, play_half or half
    if play_outs == 3 and ended_inning and ended_half in {"TOP", "BOTTOM"}:
        next_inning = ended_inning + (ended_half == "BOTTOM")
        next_half = "BOTTOM" if ended_half == "TOP" else "TOP"
        next_batting_team = home_id if next_half == "BOTTOM" else away_id
        # Explicit batting-team identity plus both next participants establishes
        # the new half even when linescore.outs/inningHalf still lag behind.
        next_pair = (_dig(defense, "pitcher", "fullName"), _dig(offense, "batter", "fullName"))
        if (next_batting_team is not None and _dig(offense, "team", "id") == next_batting_team
                and all(next_pair) and all(pair)
                and next_pair[0] != pair[0] and next_pair[1] != pair[1]
                and about.get("isComplete") is True):
            snapshot.update(inning=next_inning, inning_half=next_half, outs=0,
                            balls=0, strikes=0, pitcher_name=next_pair[0], batter_name=next_pair[1])
            return snapshot

    # No reliable next-half evidence: retain the ended play's participants,
    # count, bases and half, not linescore's possibly advanced offense/defense.
    if not play_inning or not play_half:
        # Without play-half metadata even the old participant pair cannot be
        # tied reliably to the displayed half. Do not invent that association.
        snapshot.update(pitcher_name=None, batter_name=None, balls=None, strikes=None,
                        runner_on_first=False, runner_on_second=False, runner_on_third=False)
        return snapshot
    snapshot.update(inning=ended_inning, inning_half=ended_half or None,
                    outs=play_outs if play_outs is not None else outs,
                    pitcher_name=pair[0], batter_name=pair[1])
    for base, field in (("First", "first"), ("Second", "second"), ("Third", "third")):
        snapshot[f"runner_on_{field}"] = bool(matchup.get(f"postOn{base}"))
    for side in ("away", "home"):
        runs = _int_or_none(score_result.get(f"{side}Score"))
        if runs is not None:
            snapshot[f"{side}_runs"] = runs
    return snapshot


def normalize_game(feed: dict[str, Any], schedule: dict[str, Any] | None = None) -> GameSummary:
    schedule = schedule or {}
    game_data = feed.get("gameData") or {}
    live_data = feed.get("liveData") or {}
    status_payload = game_data.get("status") or schedule.get("status") or {}
    classified = classify_game_state(status_payload)
    away = _team_values(feed, schedule, "away")
    home = _team_values(feed, schedule, "home")
    linescore = live_data.get("linescore") or {}
    current_play = live_data.get("plays", {}).get("currentPlay") or {}
    decisions = live_data.get("decisions") or {}
    winner = decisions.get("winner") or {}
    loser = decisions.get("loser") or {}
    save = decisions.get("save") or {}
    winner_stats = _decision_pitching_stats(live_data, winner)
    loser_stats = _decision_pitching_stats(live_data, loser)
    save_stats = _decision_pitching_stats(live_data, save)
    scheduled_time = _parse_datetime(_dig(game_data, "datetime", "dateTime") or schedule.get("gameDate"))
    season = _int_or_none(_dig(game_data, "game", "season") or schedule.get("season"))
    snapshot = _live_snapshot(linescore, current_play, away[0], home[0],
                              _dig(live_data, "plays", "allPlays", default=[]))

    return GameSummary(
        game_pk=_int_or_none(_dig(game_data, "game", "pk") or schedule.get("gamePk")),
        state=classified.state,
        status=classified.display_status,
        uses_live_layout=classified.uses_live_layout,
        is_final=classified.is_final,
        is_pregame=classified.is_pregame,
        away_team_id=away[0], away_team_name=away[1], away_abbreviation=away[2],
        away_runs=snapshot.pop("away_runs", away[3]), away_hits=away[4], away_errors=away[5],
        away_starting_pitcher=away[6],
        away_starting_pitcher_wins=away[7], away_starting_pitcher_losses=away[8],
        home_team_id=home[0], home_team_name=home[1], home_abbreviation=home[2],
        home_runs=snapshot.pop("home_runs", home[3]), home_hits=home[4], home_errors=home[5],
        home_starting_pitcher=home[6],
        home_starting_pitcher_wins=home[7], home_starting_pitcher_losses=home[8],
        **snapshot,
        scheduled_time=scheduled_time,
        ended_at=_terminal_play_end(live_data) if classified.is_final else None,
        official_date=str(_dig(game_data, "datetime", "officialDate") or schedule.get("officialDate") or "") or None,
        venue=_dig(game_data, "venue", "name") or _dig(schedule, "venue", "name"),
        winning_pitcher=winner.get("fullName"),
        winning_pitcher_wins=_int_or_none(winner_stats.get("wins")),
        winning_pitcher_losses=_int_or_none(winner_stats.get("losses")),
        losing_pitcher=loser.get("fullName"),
        losing_pitcher_wins=_int_or_none(loser_stats.get("wins")),
        losing_pitcher_losses=_int_or_none(loser_stats.get("losses")),
        save_pitcher=save.get("fullName"),
        save_pitcher_saves=_int_or_none(save_stats.get("saves")),
        season=season,
    )
