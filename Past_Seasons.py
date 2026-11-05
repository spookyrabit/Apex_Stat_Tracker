"""
Your Google Sheets for past seasons -> one table the Control Panel reads (Past_Seasons/past_games.csv).

    .venv/bin/python Past_Seasons.py

Reads "Past Games" from every Past_Seasons/Apex Game Data S<n>.xlsx (Google Sheets: File -> Download ->
Microsoft Excel). The columns moved around from season to season, so they're found by their heading. Only the
seasons the scripts don't write to themselves are taken (Seasons.SEASON_STARTS: S30 on come from their .ods).
Run it again after adding or changing a file; the .xlsx files are never changed.
"""
import csv
import re
import sys
from datetime import datetime, time, timedelta

import openpyxl

import Seasons
from Extract_Stats import LEGENDS

FIELDS = ["season", "time", "date_only", "placement", "squad_kills", "kills", "assists", "knocks", "survival",
          "revives", "respawns", "damage", "legend", "death_zone", "mode", "map", "friends", "mate_1", "mate_2",
          "entry", "poi", "contested", "rp", "percentile", "skill"]
# heading in the sheet (any of the spellings used) -> field
HEADINGS = {"squad_kills": ["Squad Kills"], "kills": ["Kills"], "assists": ["Assists"], "knocks": ["Knocks"],
            "revives": ["Revives"], "respawns": ["Respawns"], "damage": ["Damage"], "legend": ["Legend"],
            "death_zone": ["Death Zone"], "mode": ["Game Mode"], "map": ["Map"], "friends": ["Freinds", "Friends"],
            "entry": ["Entery", "Entry"], "poi": ["POI"], "contested": ["Contested"], "rp": ["Ranked RP"],
            "percentile": ["Percentile"], "skill": ["Skill"]}     # S27: Percentile only (your team's); S28 on: "start-team-end"
MODES = {"trios": "Trios", "duos": "Duos", "dues": "Duos", "ranked": "Ranked", "wildcard": "Wildcard"}
SPELLINGS = {"gibralter": "Gibraltar", "mad magie": "Mad Maggie", "newcastle": "New Castle"}   # typed wrong in the sheets
MAPS = ["Worlds Edge", "E-District", "Olympus", "Broken Moon", "Kings Canyon", "Storm Point"]
NUMBERS = {"placement", "squad_kills", "kills", "assists", "knocks", "revives", "respawns", "damage", "entry", "rp",
           "skill"}


def seconds(value):
    """Survival time as seconds (the sheets hold durations, times of day or nothing)."""
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    return None


def tidy(field, value):
    if isinstance(value, str):
        value = value.strip() or None
    if value is None:
        return None
    if field in NUMBERS:
        return value if isinstance(value, (int, float)) else None
    if field == "mode":
        return MODES.get(value.lower(), value)
    if field == "map":
        return next((m for m in MAPS if m.lower() == value.lower()), value)
    if field in ("legend", "mate_1", "mate_2"):
        return SPELLINGS.get(value.lower()) or next((l for l in LEGENDS if l.lower() == value.lower()), value)
    if field == "percentile":
        return str(round(value)) if isinstance(value, (int, float)) else value
    if field == "contested":
        return {"y": "Yes", "yes": "Yes", "n": "No", "no": "No"}.get(value.lower())
    return value


def read_season(path, season):
    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True)["Past Games"]
    rows = list(sheet.iter_rows(max_col=40, values_only=True))
    header = [h.strip() if isinstance(h, str) else h for h in rows[0]]
    column = {field: next((header.index(n) for n in names if n in header), None) for field, names in HEADINGS.items()}
    times = [i for i, h in enumerate(header) if h == "Time"]
    # When the match ended: the second "Time" (the first is survival); in S23-24 an unnamed column before Most Damage?
    when = times[1] if len(times) > 1 else header.index("Most Damage?") - 1
    friends = column["friends"]
    column["mate_1"], column["mate_2"] = friends + 1, friends + 2      # the teammates' legends, unnamed
    games = []
    for row in rows[1:]:
        if not isinstance(row[0], (int, float)):
            continue
        g = {"season": season, "placement": row[0], "survival": seconds(row[times[0]])}
        g.update({f: tidy(f, row[c]) if c is not None else None for f, c in column.items()})
        ended = row[when]
        g["time"] = ended if isinstance(ended, datetime) else None
        # Some early rows only have a day typed in (midnight on the dot; the real ones carry milliseconds)
        g["date_only"] = g["time"] is not None and g["time"].time() == time()
        games.append(g)
    # Rows with no date at all get the day of the game next to them, so they keep their place in the season
    for i, g in enumerate(games):
        if g["time"] is None:
            near = next((games[j]["time"] for j in list(range(i + 1, len(games))) + list(range(i - 1, -1, -1))
                         if games[j]["time"] is not None), None)
            g["time"], g["date_only"] = datetime.combine(near.date(), time()), True
    return games


def main():
    files = sorted(Seasons.PAST_SEASONS.glob("Apex Game Data S*.xlsx"))
    games = []
    for path in files:
        season = int(re.search(r"S(\d+)", path.stem).group(1))
        if season >= min(Seasons.SEASON_STARTS):
            continue                       # written by the scripts: read from its .ods
        found = read_season(path, season)
        print(f"S{season}: {len(found)} games ({sum(g['date_only'] for g in found)} with only a date), "
              f"{min(g['time'] for g in found):%Y-%m-%d} to {max(g['time'] for g in found):%Y-%m-%d}")
        games += found
    if not games:
        sys.exit(f"No past seasons found in {Seasons.PAST_SEASONS}")
    with open(Seasons.PAST_GAMES, "w", newline="") as f:
        out = csv.DictWriter(f, fieldnames=FIELDS)
        out.writeheader()
        for g in games:
            out.writerow({**g, "time": g["time"].isoformat(timespec="seconds"), "date_only": int(g["date_only"])})
    print(f"Wrote {len(games)} games to {Seasons.PAST_GAMES.relative_to(Seasons.HERE)}")
    unknown = sorted({g[f] for g in games for f in ("legend", "mate_1", "mate_2")
                      if g[f] and g[f] not in LEGENDS and g[f] != "Empty"})
    if unknown:
        print("Legends not in Extract_Stats.LEGENDS (left as typed):", ", ".join(unknown))


if __name__ == "__main__":
    main()
