"""
Which Apex season a match belongs to, and where that season's games are kept.

    Apex Game Data S<n>.ods      - a season the scripts write to (one workbook per season)
    Past_Seasons/*.xlsx          - your Google Sheets for older seasons, as downloaded (never changed)
    Past_Seasons/past_games.csv  - their Past Games in one table, made by Past_Seasons.py (the panel reads it)

A new season: add its start below, then `.venv/bin/python Sheet_Write.py --new-season` makes its workbook
(a copy of the last one with the games taken out). Matches go into the workbook of the season they were
played in, so a match from the night before still goes into the old one.
"""
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
PAST_SEASONS = HERE / "Past_Seasons"
PAST_GAMES = PAST_SEASONS / "past_games.csv"

# When each season the scripts write to started (your local time: seasons start 10 am Pacific = 1 pm Eastern).
# The first one holds everything captured before the second.
SEASON_STARTS = {
    30: datetime.min,
    31: datetime(2026, 11, 3, 13, 0),
}


def season_at(when):
    """The season `when` (a datetime) falls in."""
    return max(s for s, start in SEASON_STARTS.items() if start <= when)


def season_of_match(name):
    """The season of a Captures folder (named by when the match started: YYYY-MM-DD_HH-MM-SS)."""
    return season_at(datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S"))


def current():
    return season_at(datetime.now())


def workbook(season=None):
    """The workbook of a season (default: the one being played now)."""
    return HERE / f"Apex Game Data S{season or current()}.ods"


def workbooks():
    """{season: path} for every season workbook that exists, newest first."""
    return {s: workbook(s) for s in sorted(SEASON_STARTS, reverse=True) if workbook(s).exists()}
