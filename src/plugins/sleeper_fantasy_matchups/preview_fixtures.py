"""Offline dashboard fixtures for visual development and tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from .models import DashboardModel, FantasyMatchup, MatchupSide, MatchupStatus, SleeperUser

NOW = datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)
USER = SleeperUser("100", "gridiron_guru", "Gridiron Guru")


def side(
    roster_id, name, score, record="1-0", projected=118.4,
    remaining=34.2, probability=None, starters=3,
):
    return MatchupSide(
        roster_id=roster_id,
        team_name=name,
        owner_name=name,
        record=record,
        current_score=score,
        projected_final=projected,
        estimated_remaining_points=remaining,
        win_probability=probability,
        starters_remaining=starters,
        remaining_variance=80.0,
    )


def matchup(
    league_id="1", league_name="Sunday Night Syndicate",
    status=MatchupStatus.LIVE, user_score=84.7, opponent_score=80.2,
    user_probability=0.58, user_projected=121.5, opponent_projected=116.8,
    user_name="Fourth & Long", opponent_name="End Zone Empire",
    projections=True, week=1, playoff=False,
):
    user = side(2, user_name, user_score, "1-0", user_projected, max(0, user_projected-user_score), user_probability, 3)
    opponent = side(8, opponent_name, opponent_score, "0-1", opponent_projected, max(0, opponent_projected-opponent_score), None if user_probability is None else 1-user_probability, 4)
    if not projections:
        for value in (user, opponent):
            value.projected_final = None
            value.estimated_remaining_points = None
            value.win_probability = None
            value.starters_remaining = None
            value.remaining_variance = None
    return FantasyMatchup(
        league_id, league_name, week, status, user, opponent, NOW,
        projections_available=projections, is_playoff=playoff,
    )


def dashboard(*matchups):
    return DashboardModel(USER, "2026", 1, list(matchups))


def preview_states():
    close = matchup()
    favorite = matchup(
        "2", "Dynasty After Dark", user_score=112.8, opponent_score=71.4,
        user_probability=.88, user_projected=147.2, opponent_projected=105.6,
        user_name="The Waiver Wizards", opponent_name="Sunday Scaries",
    )
    final = matchup(
        "3", "Neighborhood League", MatchupStatus.FINAL,
        137.5, 129.1, 1.0, 137.5, 129.1,
        "Gridiron Guild", "Blitz Brigade",
    )
    pregame = matchup(
        "4", "Office Gridiron", MatchupStatus.PREGAME,
        0, 0, .53, 116.4, 114.7, "Monday Miracles", "Goal Line Stand",
    )
    bye_user = side(12, "Bye Week Bandits", 0, "2-0", None, None, None, None)
    bye = FantasyMatchup("5", "Keeper Kingdom", 1, MatchupStatus.BYE, bye_user, None, NOW)
    no_matchup = FantasyMatchup(
        "12", "Schedule Pending", 1, MatchupStatus.NO_MATCHUP, last_updated=NOW,
    )
    failed = FantasyMatchup(
        "6", "League Temporarily Offline", 1, MatchupStatus.UNAVAILABLE,
        last_updated=NOW, error_state="Sleeper matchup data is temporarily unavailable.",
    )
    unavailable = matchup("7", "Projection-Free League", projections=False)
    long_names = matchup(
        "8", "The Exceptionally Long International Fantasy Football Championship",
        user_score=1004.25, opponent_score=987.75,
        user_name="The Incredibly Long Team Name That Must Never Overlap",
        opponent_name="Another Unreasonably Lengthy Fantasy Franchise Name",
    )
    long_names_alt = matchup(
        "11", "Commissioner's Extremely Long Sunday Dynasty Invitational",
        user_score=148.65, opponent_score=139.2, user_probability=.64,
        user_name="Waiver Wire Archaeologists and Touchdown Enthusiasts",
        opponent_name="The Perpetually Questionable Game-Time Decisions",
    )
    states = {
        "one_close_live": dashboard(close),
        "one_strong_favorite": dashboard(favorite),
        "one_final": dashboard(final),
        "two_live": dashboard(close, favorite),
        "two_live_final": dashboard(close, final),
        "three_mixed": dashboard(close, final, pregame),
        "four_live": dashboard(close, favorite, matchup("9", "Rivalry Week"), matchup("10", "The Flex Zone")),
        "four_mixed": dashboard(close, final, pregame, bye),
        "four_one_failed": dashboard(close, favorite, failed, pregame),
        "long_names_high_scores": dashboard(long_names),
        "two_long_names": dashboard(long_names, long_names_alt),
        "three_long_names": dashboard(long_names, long_names_alt, long_names),
        "four_long_names": dashboard(long_names, long_names_alt, long_names, long_names_alt),
        "projections_unavailable": dashboard(unavailable),
        "one_no_matchup": dashboard(no_matchup),
        "four_projections_unavailable": dashboard(unavailable, unavailable, unavailable, unavailable),
    }
    return {name: deepcopy(value) for name, value in states.items()}
