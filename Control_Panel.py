"""
The tracker's window: your stats and the capture controls in one place.

Filters along the top (season, mode, map, legend, time) apply to every tab but the first:
    Latest    the newest match: the live map big, and underneath who's playing what, then their stats
    Overview  totals and averages, best games, and a breakdown by legend / map / POI / squad / friends...
    Squad     the same for every player in your squads (Squad Stats): you vs teammates, friends, randoms
    Charts    graphs: your averages by lobby skill / rank (Skill distribution), by legend / map... (Compare
              groups), a stat's running average (Over time), ranked RP through each season
    Matches   every game in the workbook; pick one to see its squad, match picture and stats screen
    Maps      each map's heatmap (everyone / just you) and where the endgame rings ended up
    Capture   is the capture running, restart / stop it, the workbook commands and the live capture.log

Stats are read straight from every season's workbook (read-only, fine while it's open in Calc) plus the past
seasons from your Google Sheets (Past_Seasons/past_games.csv, see Past_Seasons.py), and re-read whenever one
changes, so a match shows up a minute or so after it's written.

Usage:
    .venv/bin/python Control_Panel.py
"""
import csv
import json
import math
import queue
import re
import shutil
import subprocess
import threading
import time
import zlib
import tkinter as tk
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator, MultipleLocator
from PIL import Image, ImageTk

import Extract_Stats
import Map_View
import Match_Map
import Movement
import Screen_Notes
import Seasons

# ---------------------------
# CONFIGURATION
# ---------------------------

HERE = Path(__file__).parent
PYTHON = HERE / ".venv" / "bin" / "python"
CAPTURES = HERE / "Captures"
LOG = CAPTURES / "capture.log"
LATEST_MAP = HERE / "Latest Match Map.png"
LATEST_ZOOM = HERE / "Latest Match Map Zoomed.png"
LIVE_RINGS = CAPTURES / "live_rings.json"     # the live map's rings (Match_Map.LiveMap), for the endgame guess
MAPS = HERE / "Maps"
SERVICE = "apex-stat-capture"
RESTART_LATER = "apex-restart-later"     # the unit the README uses for ./restart_capture.sh --after-match
LOG_LINES = 600                          # capture.log lines kept in the window
MATCH_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

# Past Games columns (letters as in the README) -> field names used here
GAME_COLUMNS = {"A": "placement", "B": "squad_kills", "C": "kills", "D": "assists", "E": "knocks", "F": "survival",
                "G": "revives", "H": "respawns", "I": "damage", "J": "legend", "K": "time", "L": "damage_share",
                "M": "death_zone", "O": "mode", "P": "map", "Q": "friends", "R": "mate_1", "S": "mate_2",
                "T": "entry", "U": "poi", "V": "contested", "W": "rank_distribution", "X": "lobby_position", "AA": "percentile", "AB": "skill",
                "AH": "rp", "AI": "rp_real"}
# Layers on the Maps tab, bottom to top, stacked over the map: file in Maps/<Map>/ (transparent, Heatmap.Layer), button,
# what it shows, on at first. A new layer: draw it with Heatmap.Layer, add it here. POI names go on top of them all.
# A layer file is a picture (what's smooth) and marks (shapes: Map_View); each can be its own button. Bottom to top:
# (file in Maps/<Map>/, part: "picture" / "marks" / "both", group (buttons side by side under one name) or None,
# button, what it shows, on at first). A new layer: draw it with Heatmap.Layer, add it here. POI names go on top.
MAP_LAYERS = [("layer_land.png", "picture", "Ring land", "Areas",
               "Ring land areas: where players can go (the endgame guess's land). White = where your squads and the "
               "players you spectated after them walked (skydives, jump towers and balloons left out). Green = playable: "
               "where 8+ games walked nearby, only right next to a path, so mountains and walls your squads go round are "
               "out; dim green = quiet (fewer games nearby), counted playable. Red = the map's thick walls / tall buildings "
               "(E-District's purple): not playable unless a path is on them. Blue = gaps the route may cross.", False),
              ("layer_land.png", "marks", "Ring land", "Line",
               "Ring land line: yellow = the last ring's centre can be inside it (70%+ of the ring on playable ground, not "
               "on a wall).", False),
              ("layer_heatmap.png", "picture", "Heatmap", "Heat",
               "Heat: how many player-trips passed each spot (your whole squad and the players you spectated after it "
               "was out, on foot): blue = sometimes, red = most often.", True),
              ("layer_heatmap.png", "marks", "Heatmap", "Landings",
               "Landings: a green dot where your squad landed, every game.", True),
              ("layer_rings.png", "both", None, "Endgame rings",
               "Endgame rings: where endgames ended up (the last 3 ring sizes, one colour per round). Thin line = one "
               "game's ring; a filled blob + number = that many games' rings in the same spot.", False),
              ("layer_deaths.png", "picture", "Deaths", "Heat",
               "Deaths heat: where you went down for good (red; wins left out).", False),
              ("layer_deaths.png", "marks", "Deaths", "Spots",
               "Death spots: a red dot where you died, a small orange dot where you were downed and came back.", False),
              ("layer_ring_sizes.png", "marks", None, "Ring sizes",
               "Ring sizes: one of your endgames at a time (a new one every 4 s): its rings at their round's set size, "
               "each with its round and metres; the map's sizes top left.", False)]
# Item layers (Heatmap.item_heatmaps): their own buttons on the Maps tab, each showing the item's icon (Match_Map.draw_item)
ITEM_LAYERS = [("layer_care_package.png", "care_package", "Care Packages",
                "Care Packages: where they turned up in your games (off the map and minimaps, read from 2026-11-05 on: "
                "S31's marker and the orange circle). White heat = more games there, a dot on each one."),
               ("layer_stronghold.png", "stronghold", "Strongholds",
                "Strongholds: where they were in your games (read from 2026-11-05 on). Pink-red heat = more games there, "
                "a dot on each one."),
               ("layer_evo_harvester.png", "evo_harvester", "Evo Harvesters",
                "Evo Harvesters: where they were in your games (off the minimaps). Yellow heat = more games there, a dot "
                "on each one.")]
MAP_DIM = 0.45                     # the map under the layers is darkened this much, so they stand out
LAYER_VIEWS = CAPTURES / ".notes" / "maps"     # the stacked map as an SVG (Open in browser)
PERIODS = ["All time", "Today", "Last 7 days", "Last 30 days", "Last 50 games", "Last 200 games"]
ALL = "All"

# Dark theme
BG = "#16181d"            # window
SURFACE = "#1d2027"       # tables, inputs, the log
RAISED = "#272b34"        # tiles, buttons, headings
HOVER = "#323743"
BORDER = "#3a3f4b"
TEXT_COLOUR = "#e4e7ed"
MUTED = "#a3aab7"         # secondary text
FAINT = "#727a88"         # hints
ACCENT = "#5b9cf0"
SELECTED = "#2d5288"
ROW_ALT = "#22262e"       # every other table row


# ---------------------------
# READING THE WORKBOOK
# ---------------------------

TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def cell_value(cell):
    """A cell as Python: number, datetime, seconds (durations), or text."""
    kind = cell.get(OFFICE + "value-type")
    if kind in ("float", "percentage", "currency"):
        return float(cell.get(OFFICE + "value"))
    if kind == "date":
        return datetime.fromisoformat(cell.get(OFFICE + "date-value"))
    if kind == "time":
        h, m, s = re.match(r"-?PT(\d+)H(\d+)M([\d.]+)S", cell.get(OFFICE + "time-value")).groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    text = "\n".join("".join(p.itertext()) for p in cell.findall(TEXT + "p"))
    return text or None


WORKBOOK_CACHE = CAPTURES / ".notes" / "workbooks"   # each workbook's cells, parsed (~1.3 s a workbook to read)


def read_workbook(path):
    """{tab name: [row values]} from the .ods, kept parsed in WORKBOOK_CACHE until the file changes (only this
    season's workbook changes, after each match)."""
    import pickle
    path = Path(path)
    stamp = (path.stat().st_size, path.stat().st_mtime_ns)
    cache = WORKBOOK_CACHE / f"{path.stem}.pickle"
    try:
        saved_stamp, tabs = pickle.loads(cache.read_bytes())
        if saved_stamp == stamp:
            return tabs
    except (OSError, EOFError, pickle.UnpicklingError, ValueError):
        pass
    tabs = parse_workbook(path)
    try:
        WORKBOOK_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f".{cache.name}.tmp")
        tmp.write_bytes(pickle.dumps((stamp, tabs)))
        tmp.replace(cache)
    except OSError:
        pass
    return tabs


def parse_workbook(path):
    """{tab name: [row values]} from the .ods (it's a zip with the cells in content.xml)."""
    tabs = {}
    with zipfile.ZipFile(path) as z, z.open("content.xml") as f:
        for _, el in ET.iterparse(f):
            if el.tag != TABLE + "table":
                continue
            rows = []
            for row in el.iter(TABLE + "table-row"):
                values = []
                for cell in row:
                    if cell.tag in (TABLE + "table-cell", TABLE + "covered-table-cell"):
                        values += [cell_value(cell)] * min(int(cell.get(TABLE + "number-columns-repeated", 1)), 64)
                while values and values[-1] is None:
                    values.pop()
                rows += [values] * (min(int(row.get(TABLE + "number-rows-repeated", 1)), 2) if values else 1)
            tabs[el.get(TABLE + "name")] = rows
            el.clear()
    return tabs


def column_index(letters):
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch) - 64
    return index - 1


def sources():
    """Every file the games come from: each season's workbook, and the past seasons' table."""
    return list(Seasons.workbooks().values()) + ([Seasons.PAST_GAMES] if Seasons.PAST_GAMES.exists() else [])


def sources_saved():
    """When the files were last saved (to notice a change)."""
    return tuple(path.stat().st_mtime for path in sources())


def load_stats():
    """Every game of every season as a dict (newest first): the season workbooks' Past Games, each with its squad
    from Squad Stats when the match was captured, and the past seasons from your Google Sheets."""
    games = []
    for season, path in Seasons.workbooks().items():
        games += load_workbook(path, season)
    games += load_past_seasons()
    games.sort(key=lambda g: g["time"], reverse=True)
    name_players(games)
    add_measures(games)
    add_movement(games)
    return games


MOVEMENT = ("glide", "walked", "pace", "spread", "together", "alone", "death_gap", "last_up")


def add_movement(games):
    """From each captured match's trails.json (Movement.py, cached): "glide" (m from your jump to where you landed;
    None in Ranked - Dropzone), "walked" (m on foot after landing), "pace" (m a minute of that), "spread" (m to your
    nearest teammate, the game's median), "together" / "alone" (share of the game within Movement.TOGETHER_M of one
    / further than Movement.ALONE_M from all), "death_gap" (m to your nearest teammate in your last fight) and
    "last_up" (no teammate seen in it). A win has no last fight: its trails.json "death" is just where you were."""
    found = Movement.for_matches([g["match"] for g in games if g["match"]])
    for g in games:
        m = found.get(g["match"]) or {}
        g.update({key: m.get(key) for key in MOVEMENT})
        if g["placement"] == 1:
            g["death_gap"] = g["last_up"] = None
        g["together_pct"] = g["together"] * 100 if g["together"] is not None else None     # 0-100, for grouping
        squad_movement(g["squad"], m.get("players") or [])


PLAYER_MOVEMENT = ("walked", "pace", "spread", "together", "gap_me", "near_me")
TRAIL_NAME_MATCH = 0.6          # name_similarity a squad panel name (trails.json) needs to an end card name


def squad_movement(squad, tracked):
    """Each Squad Stats row gets its player's movement (Movement.py "players"): you by Is Me, a teammate by the
    squad panel name closest to the end card's (each trail used once) - or, when the panel's name wasn't read
    ("Teammate (green)"), the one trail left for the one teammate left - and "kp", their kill participation."""
    left = [t for t in tracked if not t.get("is_me")]
    found = {}
    for i, p in enumerate(squad):
        if p.get("Is Me") == "Yes":
            found[i] = next((t for t in tracked if t.get("is_me")), {})
            continue
        scored = [(Extract_Stats.name_similarity(p.get("Player"), t.get("name")), t) for t in left]
        score, numbers = max(scored, key=lambda st: st[0], default=(0, {}))
        if score >= TRAIL_NAME_MATCH:
            left.remove(numbers)
            found[i] = numbers
    unmatched = [i for i, p in enumerate(squad) if i not in found]
    if len(unmatched) == 1 and len(left) == 1:
        found[unmatched[0]] = left[0]
    for i, p in enumerate(squad):
        p.update({key: found.get(i, {}).get(key) for key in PLAYER_MOVEMENT})
        parts = [p.get(k) for k in ("Kills", "Assists", "Squad Kills")]
        p["kp"] = (min((parts[0] + parts[1]) / parts[2], 1.0)
                   if all(isinstance(v, (int, float)) for v in parts) and parts[2] > 0 else None)


def load_past_seasons():
    """The past seasons from Past_Seasons/past_games.csv (made by Past_Seasons.py), in the same shape as the
    workbook's games (no squads: they weren't captured)."""
    if not Seasons.PAST_GAMES.exists():
        return []
    games = []
    with open(Seasons.PAST_GAMES, newline="") as f:
        for row in csv.DictReader(f):
            g = {field: None for field in GAME_COLUMNS.values()}
            for field, value in row.items():
                if field in g:
                    g[field] = value or None
            for field in ("placement", "squad_kills", "kills", "assists", "knocks", "survival", "revives",
                          "respawns", "damage", "entry", "rp", "rp_real", "skill"):
                g[field] = float(g[field]) if g[field] else None
            g.update(time=datetime.fromisoformat(row["time"]), season=int(row["season"]),
                     date_only=row["date_only"] == "1", squad=[], match=None)
            g["mates"] = [m for m in (g["mate_1"], g["mate_2"]) if m and m != "Empty"]
            games.append(g)
    return games


def load_workbook(path, season):
    tabs = read_workbook(path)
    squads = defaultdict(list)
    squad_rows = tabs.get("Squad Stats", [])
    header = squad_rows[0] if squad_rows else []
    for row in squad_rows[1:]:
        player = dict(zip(header, row + [None] * (len(header) - len(row))))     # blank cells at the end aren't read
        if isinstance(player.get("Time"), datetime):
            squads[player["Time"].replace(microsecond=0)].append(player)
    games = []
    for row in tabs.get("Past Games", [])[1:]:
        if not row or not isinstance(row[0], float):
            continue
        g = {field: row[column_index(col)] if column_index(col) < len(row) else None
             for col, field in GAME_COLUMNS.items()}
        if not isinstance(g["time"], datetime):
            continue
        g["mates"] = [m for m in (g["mate_1"], g["mate_2"]) if isinstance(m, str) and m != "Empty"]
        g["squad"] = sorted(squads.get(g["time"].replace(microsecond=0), []), key=lambda p: p.get("Slot") or 0)
        g["match"] = g["squad"][0]["Match"] if g["squad"] else None
        g["season"], g["date_only"] = season, False
        if not g["match"]:              # hand-typed rows used L for a 0 / 1 / 2 mark, not a share of damage
            g["damage_share"] = None
        games.append(g)
    return games


def name_players(games):
    """Give every Squad Stats row a steady player name ("who": "You", or the commonest spelling of that tag;
    the end card cuts names off and OCR slips letters, so "nutzenhar" is "nutzenhammer"), who else was in
    the squad ("with"), whether they're a friend, and whether they did the most damage in the squad ("top")."""
    spellings = Counter(p["Player"] for g in games for p in g["squad"] if p.get("Is Me") != "Yes" and p.get("Player"))
    groups = []                     # spellings of one tag, commonest first
    for name, _ in spellings.most_common():
        group = next((g for g in groups if any(Extract_Stats.names_alike(name, n) for n in g)), None)
        group.append(name) if group else groups.append([name])
    steady = {name: g[0] for g in groups for name in g}
    friends = list(Extract_Stats.FRIENDS)
    for g in games:
        top = max((p.get("Damage") or 0 for p in g["squad"]), default=0)
        for p in g["squad"]:
            p["who"] = "You" if p.get("Is Me") == "Yes" else steady.get(p.get("Player"), "(no name)")
            p["friend"] = p["who"] != "You" and any(Extract_Stats.names_alike(p["who"], f) for f in friends)
            p["top"] = bool(top) and (p.get("Damage") or 0) == top
        for p in g["squad"]:
            p["with"] = [q["who"] for q in g["squad"] if q is not p]


TIERS = Extract_Stats.RANKS              # Rookie ... Apex Predator: tier 0-7
DIVISIONS = ["IV", "III", "II", "I"]     # a quarter of a tier each


def rank_value(rank):
    """A rank as a number: "Silver III" -> 2.25 (the tier, plus a quarter per division above IV)."""
    if not isinstance(rank, str):
        return None
    if rank in TIERS:                    # Master, Apex Predator: no divisions
        return float(TIERS.index(rank))
    tier, _, division = rank.rpartition(" ")
    if tier not in TIERS or division not in DIVISIONS:
        return None
    return TIERS.index(tier) + DIVISIONS.index(division) / 4


def rank_name(value):
    """2.25 -> "Silver III" (rank_value backwards)."""
    tier = min(int(value + 1e-9), len(TIERS) - 1)
    if TIERS[tier] in ("Master", "Apex Predator"):
        return TIERS[tier]
    return f"{TIERS[tier]} {DIVISIONS[min(int((value - tier) * 4 + 1e-9), 3)]}"


def lobby_counts(distribution):
    """"Bronze 3, Silver 17" -> [(1, 3), (2, 17)]: (tier index, squads) off the Rank Distribution cell."""
    return [(TIERS.index(t.strip()), int(n)) for t, n in re.findall(r"([A-Za-z ]+?)\s+(\d+)", distribution or "")
            if t.strip() in TIERS]


def lobby_rank(distribution):
    """"Bronze 3, Silver 17" -> 2.35 (about Silver III): the Ranked lobby's average rank, each squad counted once.
    The bar only has tiers, so each counts as the middle of its tier (Silver = 2.5, rank_value's Silver II)."""
    counts = lobby_counts(distribution)
    total = sum(n for _, n in counts)
    return sum((t + 0.5) * n for t, n in counts) / total if total else None


def lobby_standing(rank, distribution):
    """Where a player's rank sits among the Ranked lobby's squads: ("Gold IV", "Silver 3, Gold 17") ->
    "3 below · 17 Gold · 0 above". The bar only has tiers, so it's squads in lower tiers, the player's, higher ones."""
    value, counts = rank_value(rank), lobby_counts(distribution)
    if value is None or not counts:
        return ""
    tier = int(value + 1e-9)
    below = sum(n for t, n in counts if t < tier)
    same = sum(n for t, n in counts if t == tier)
    above = sum(n for t, n in counts if t > tier)
    return f"{below} below · {same} {TIERS[tier]} · {above} above"


def percentile_parts(value):
    """The skill graph as the Percentile column has it: "75-80-95" (band start, your team, band end; S28 on), or
    just your team's (S27)."""
    if isinstance(value, (int, float)):
        return [float(value)]
    return [float(p) for p in re.findall(r"\d+(?:\.\d+)?", value or "")]


def add_measures(games):
    """What the Charts tab can sort games by, worked out once per game: your team's percentile and the lobby's
    Skill (the workbook's; else from the percentile, as its formula does; Ranked: its Lobby Position), the Ranked
    lobby's average tier, your rank and your squad's average rank (S31 on)."""
    for g in games:
        # Ranked RP: the real number off the EXPERIENCE tab (AI Real RP) when it was shown, else the workbook's
        # estimate (AH, from placement and kills); rp_est keeps the estimate
        g.setdefault("rp_est", g["rp"])
        g["rp_real"] = g["rp_real"] if isinstance(g.get("rp_real"), (int, float)) else None
        if g["rp_real"] is not None:
            g["rp"] = g["rp_real"]
        parts = percentile_parts(g["percentile"])
        g["team_percentile"] = parts[1] if len(parts) == 3 else parts[0] if len(parts) == 1 else None
        if not isinstance(g["skill"], (int, float)):
            g["skill"] = (2 * parts[0] + parts[1] + 4 * parts[2]) / 7 if len(parts) == 3 else None
        if g["skill"] is None and is_ranked(g):
            # Ranked has no skill graph: where your squad's marker sat on the lobby tier bar (0-100, the workbook's
            # Lobby Position) is its Lobby skill, so the skill charts take Ranked games too
            try:
                g["skill"] = float(g["lobby_position"])
            except (TypeError, ValueError):
                pass
        g["lobby_rank"] = lobby_rank(g["rank_distribution"]) if is_ranked(g) else None
        ranks = {p["who"]: rank_value(p.get("Rank")) for p in g["squad"]}
        g["my_rank"] = ranks.get("You")
        known = [r for r in ranks.values() if r is not None]
        g["squad_rank"] = sum(known) / len(known) if known else None
        scans = g["squad"][0].get("Survey Scans") if g["squad"] else None
        g["scans"] = scans if isinstance(scans, (int, float)) else None     # survey beacon scans seen on the map
        # Kill participation: the squad's kills you had a hand in, (kills + assists) / squad kills (an assist is
        # damage on someone your squad then killed, so it rarely goes over 1; capped there)
        parts = [g.get(k) for k in ("kills", "assists", "squad_kills")]
        g["kp"] = (min((parts[0] + parts[1]) / parts[2], 1.0)
                   if all(isinstance(v, (int, float)) for v in parts) and parts[2] > 0 else None)
        g["kp_pct"] = g["kp"] * 100 if g["kp"] is not None else None      # 0-100, for grouping


# ---------------------------
# NUMBERS
# ---------------------------

def mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def middle(values):
    """The median of the numbers in values (None if there are none): for distances, where one game with a teammate
    across the map would drag a mean off."""
    return Movement.median([v for v in values if isinstance(v, (int, float))])


def fmt_num(value, digits=0):
    return "–" if value is None else f"{value:,.{digits}f}"


def fmt_pct(value):
    return "–" if value is None else f"{value:.0%}"


def fmt_time(seconds):
    return "–" if seconds is None else f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def fmt_m(metres):
    return "–" if metres is None else f"{metres:,.0f} m"


def fmt_km(metres):
    return "–" if metres is None else f"{metres / 1000:.2f} km"


def fmt_when(when):
    return when.strftime("%a %d %b %H:%M") if when else ""


def is_ranked(g):
    return (g["mode"] or "").lower() == "ranked"


def summary(games):
    """Totals and per-game averages for a list of games."""
    n = len(games)
    ranked = [g for g in games if is_ranked(g)]
    known_contest = [g for g in games if g["contested"] in ("Yes", "No")]
    deaths = [g for g in games if g.get("last_up") is not None]      # a last fight in trails.json (not a win)
    return dict(
        games=n,
        wins=sum(g["placement"] == 1 for g in games),
        win_rate=sum(g["placement"] == 1 for g in games) / n if n else None,
        top5=sum(g["placement"] <= 5 for g in games) / n if n else None,
        top3=sum(g["placement"] <= 3 for g in games) / n if n else None,
        placement=mean(g["placement"] for g in games),
        kills=mean(g["kills"] for g in games), total_kills=sum(g["kills"] or 0 for g in games),
        assists=mean(g["assists"] for g in games), knocks=mean(g["knocks"] for g in games),
        damage=mean(g["damage"] for g in games), total_damage=sum(g["damage"] or 0 for g in games),
        squad_kills=mean(g["squad_kills"] for g in games),
        survival=mean(g["survival"] for g in games),
        revives=mean(g["revives"] for g in games), respawns=mean(g["respawns"] for g in games),
        damage_share=mean(g["damage_share"] for g in games),
        rp=sum(g["rp"] or 0 for g in ranked), rp_per_game=mean(g["rp"] for g in ranked), ranked=len(ranked),
        contested=sum(g["contested"] == "Yes" for g in known_contest) / len(known_contest) if known_contest else None,
        skill=mean(g.get("skill") for g in games), team_percentile=mean(g.get("team_percentile") for g in games),
        kp=mean(g.get("kp") for g in games),
        # movement (captured games with map positions, Movement.py)
        glide=mean(g.get("glide") for g in games), walked=mean(g.get("walked") for g in games),
        pace=mean(g.get("pace") for g in games), spread=middle(g.get("spread") for g in games),
        together=mean(g.get("together") for g in games), alone=mean(g.get("alone") for g in games),
        death_gap=middle(g.get("death_gap") for g in deaths),
        last_up=sum(bool(g["last_up"]) for g in deaths) / len(deaths) if deaths else None,
        moved=sum(g.get("walked") is not None for g in games),
    )


def band(value, size, top, unit):
    """A number as its group of `size` for the breakdowns: 37 -> "30-40 m"; from `top` up one group, "100+ m"
    (None stays None)."""
    if value is None:
        return None
    low = int(value // size * size)
    if low >= top:
        return f"{top}+{unit}"
    return f"{low}-{low + size}{unit}"


LAST_FIGHT_BANDS = [(15, "within 15 m"), (30, "15-30 m"), (50, "30-50 m"), (100, "50-100 m")]


def last_fight_group(g):
    """How far your nearest teammate was in your last fight, as a band (Breakdown / Compare groups)."""
    if g["placement"] == 1:
        return "(won)"
    if g.get("last_up"):
        return "last of the squad up"
    gap = g.get("death_gap")
    if gap is None:
        return None
    return next((name for top, name in LAST_FIGHT_BANDS if gap < top), "100 m+")


# what the Overview's breakdown can group games by
GROUPS = {
    "Season": lambda g: [f"S{g['season']}"],
    "Legend": lambda g: [g["legend"]],
    "Map": lambda g: [g["map"]],
    "Game mode": lambda g: [g["mode"]],
    "Landing (POI)": lambda g: [g["poi"]],
    "Death zone": lambda g: [g["death_zone"]],
    "Teammate legend": lambda g: g["mates"] or ["(solo)"],
    "Squad (all 3 legends)": lambda g: [" + ".join(sorted([g["legend"] or "?"] + g["mates"]))],
    "Friends": lambda g: [f.strip() for f in g["friends"].split(",")] if g["friends"] else ["(randoms)"],
    "Survey scans": lambda g: [f"{g['scans']:.0f} scans" if g.get("scans") is not None else None],
    "Glide distance (100s of m)": lambda g: [f"{int(g['glide'] // 100 * 100)}-{int(g['glide'] // 100 * 100) + 100} m"
                                             if g.get("glide") is not None else None],
    "Squad spread (10s of m)": lambda g: [band(g.get("spread"), 10, 100, " m")],
    "Teammate in your last fight": lambda g: [last_fight_group(g)],
    "Kill participation (10s of %)": lambda g: [band(g["kp"] * 100 if g.get("kp") is not None else None, 10, 90, "%")],
    "Hot drop (contested landing)": lambda g: [{"Yes": "Hot drop", "No": "Quiet landing"}.get(g["contested"])],
    "Lobby skill (bands of 10)": lambda g: [f"{int(min(g['skill'], 99.9) // 10 * 10)}-{int(min(g['skill'], 99.9) // 10 * 10) + 10}"
                                            if isinstance(g.get("skill"), (int, float)) else None],
    "Your rank (Ranked)": lambda g: [rank_name(g["my_rank"]) if g.get("my_rank") is not None else None],
    "Squad rank (Ranked)": lambda g: [rank_name(g["squad_rank"] // 0.25 * 0.25) if g.get("squad_rank") is not None else None],
    "Weekday": lambda g: [g["time"].strftime("%A")],
    "Hour of day": lambda g: ["(no time typed)" if g["date_only"] else g["time"].strftime("%H:00")],
    "Month": lambda g: [g["time"].strftime("%Y-%m %B")],
}


def open_path(path):
    path = Path(path)
    if path.exists():
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        messagebox.showinfo("Not found", f"{path.name} doesn't exist (yet).")


# ---------------------------
# WIDGETS
# ---------------------------

def natural(value):
    """Sort key: numbers as they are, text with the numbers in it as numbers ("100+ m" after "20-30 m",
    "S9" before "S10"); text and numbers are alternated the same way for every text, so any two compare."""
    if not isinstance(value, str):
        return value
    return [int(part) if i % 2 else part.lower() for i, part in enumerate(re.split(r"(\d+)", value))]


class Table(ttk.Frame):
    """A sortable table. columns: (key, heading, width, format, anchor); rows: dicts of raw values, so
    clicking a heading sorts by the real number, not the text."""

    def __init__(self, master, columns, height=12, on_select=None):
        super().__init__(master)
        self.columns = columns
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height,
                                 selectmode="browse")
        for key, heading, width, _, anchor in columns:
            self.tree.heading(key, text=heading, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=anchor == "w")
        self.tree.tag_configure("odd", background=ROW_ALT)
        down = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        across = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=down.set, xscrollcommand=across.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        down.grid(row=0, column=1, sticky="ns")
        across.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rows, self.shown, self.sort_key, self.reverse = [], [], None, False
        if on_select:
            self.tree.bind("<<TreeviewSelect>>", lambda e: on_select(self.selected()))

    def show(self, rows, sort_key=None, reverse=False):
        self.rows = rows
        if sort_key:
            self.sort_key, self.reverse = sort_key, reverse
        self.refresh()

    def sort(self, key):
        self.reverse = not self.reverse if key == self.sort_key else key not in (c[0] for c in self.columns[:1])
        self.sort_key = key
        self.refresh()

    def refresh(self):
        rows = self.rows
        if self.sort_key:
            known = [r for r in rows if r.get(self.sort_key) is not None]
            rows = sorted(known, key=lambda r: natural(r[self.sort_key]), reverse=self.reverse) + \
                [r for r in rows if r.get(self.sort_key) is None]                     # blanks always last
        for key, heading, *_ in self.columns:
            arrow = (" ▼" if self.reverse else " ▲") if key == self.sort_key else ""
            self.tree.heading(key, text=heading + arrow)
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(i), tags=("odd",) if i % 2 else (),
                             values=[fmt(row.get(key)) for key, _, _, fmt, _ in self.columns])
        self.shown = rows

    def selected(self):
        chosen = self.tree.selection()
        return self.shown[int(chosen[0])] if chosen else None


def text(value):
    return "" if value is None else str(value)


def lobby_position(value):
    """Ranked's Lobby Position (your squad's marker along the lobby tier bar, 0-100), shown as its Lobby skill."""
    return f"{value:.0f}" if isinstance(value, (int, float)) else text(value)


class Tooltip:
    """A small label next to a widget while the mouse is over it (for buttons that only show an icon)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _=None):
        self.hide()
        self.tip = tk.Toplevel(self.widget, background=BORDER)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        tk.Label(self.tip, text=self.text, background=RAISED, foreground=TEXT_COLOUR, padx=6, pady=2, wraplength=420,
                 justify="left").pack(padx=1, pady=1)

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class MapView(ttk.Label):
    """A map with layers you can zoom into: mouse wheel = zoom where the mouse is, drag = move, double-click = the
    whole map again. Each view is drawn fresh (Map_View.render): the layers' pictures scaled, their marks sharp."""

    def __init__(self, master, dim=1.0):
        super().__init__(master, anchor="center", cursor="fleur", text="No map picture yet")
        self.scene, self.dim, self.photo, self.pending, self.drag = None, dim, None, None, None
        self.zoom = self.centre = None                 # None: the whole map, fitted
        self.frame = None                              # which frame of a cycling layer (Ring sizes) is shown
        self.ticking = False
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-4>", lambda e: self.wheel(e, 1.25))            # (X11's wheel)
        self.bind("<Button-5>", lambda e: self.wheel(e, 0.8))
        self.bind("<MouseWheel>", lambda e: self.wheel(e, 1.25 if e.delta > 0 else 0.8))
        self.bind("<ButtonPress-1>", lambda e: setattr(self, "drag", (e.x, e.y, self.view()[0])))
        self.bind("<B1-Motion>", self.move)
        self.bind("<Double-Button-1>", lambda e: self.reset())

    def set(self, scene, keep_view=False, missing="No map picture yet"):
        """scene: (base picture, [layer pictures], marks, map size), or None."""
        self.scene = scene
        if not keep_view:
            self.zoom = self.centre = None
        if not scene:
            self.photo = None
            self.configure(image="", text=missing)
            return
        self.redraw()
        if Map_View.cycles(scene[2]) and not self.ticking:
            self.ticking = True
            self.tick()

    def tick(self):
        """While a layer cycles (Ring sizes: a new endgame every few seconds), redraw when its frame changes."""
        every = Map_View.cycles(self.scene[2]) if self.scene else None
        if not every:
            self.ticking = False
            return
        frame = int(time.time() // every)
        if frame != self.frame and self.winfo_ismapped():
            self.frame = frame
            self.redraw()
        self.after(250, self.tick)

    def size(self):
        return max(self.winfo_width() - 4, 20), max(self.winfo_height() - 4, 20)

    def fitted(self):
        w, h = self.size()
        mw, mh = self.scene[3]
        return min(w / mw, h / mh)

    def view(self):
        """(centre, zoom) shown now."""
        mw, mh = self.scene[3]
        return (self.centre or (mw / 2, mh / 2)), (self.zoom or self.fitted())

    def wheel(self, event, factor):
        if not self.scene:
            return
        (cx, cy), zoom = self.view()
        new = min(max(zoom * factor, self.fitted()), Map_View.MAX_ZOOM)
        w, h = self.size()
        # Keep the map spot under the mouse where it is
        mx, my = cx + (event.x - w / 2) / zoom, cy + (event.y - h / 2) / zoom
        self.zoom = None if new <= self.fitted() * 1.001 else new
        self.centre = None if self.zoom is None else (mx - (event.x - w / 2) / new, my - (event.y - h / 2) / new)
        self.redraw()

    def move(self, event):
        if not self.scene or not self.drag or self.zoom is None:
            return
        x0, y0, (cx, cy) = self.drag
        self.centre = (cx - (event.x - x0) / self.zoom, cy - (event.y - y0) / self.zoom)
        self.redraw()

    def reset(self):
        self.zoom = self.centre = None
        self.redraw()

    def redraw(self):
        if not self.pending:
            self.pending = self.after(15, self._draw)

    def _draw(self):
        self.pending = None
        if not self.scene or self.winfo_width() < 30:
            return
        base, layers, marks, ref_size = self.scene
        (cx, cy), zoom = self.view()
        w, h = self.size()
        if self.zoom is not None:                      # don't let the map slide out of view
            cx = min(max(cx, w / 2 / zoom), ref_size[0] - w / 2 / zoom) if ref_size[0] * zoom > w else ref_size[0] / 2
            cy = min(max(cy, h / 2 / zoom), ref_size[1] - h / 2 / zoom) if ref_size[1] * zoom > h else ref_size[1] / 2
            self.centre = (cx, cy)
        picture = Map_View.render(base, layers, marks, ref_size, (cx, cy), zoom, (w, h), self.dim)
        self.photo = ImageTk.PhotoImage(picture)
        self.configure(image=self.photo, text="")


class FittedImage(ttk.Label):
    """Shows a picture scaled to fit whatever space the label has; click to open it full size."""

    def __init__(self, master, **kw):
        super().__init__(master, anchor="center", cursor="hand2", **kw)
        self.path, self.source, self.photo, self.pending = None, None, None, None
        self.bind("<Configure>", lambda e: self.refit())
        self.bind("<Button-1>", lambda e: self.path and open_path(self.path))

    def set(self, path, missing="No picture yet"):
        self.path = path if path and Path(path).exists() else None
        try:
            self.source = Image.open(self.path).convert("RGB") if self.path else None
        except OSError:                       # half-written: try again on the next refresh
            self.source = self.path = None
        if not self.source:
            self.photo = None
            self.configure(text=missing, image="")
            return
        self.configure(text="")               # the old picture stays up until the new one is fitted (no flash)
        self.refit()

    def refit(self):
        if self.pending:
            self.after_cancel(self.pending)
        self.pending = self.after(120, self._fit)

    def _fit(self):
        self.pending = None
        if not self.source:
            return
        w, h = self.winfo_width() - 4, self.winfo_height() - 4
        if w < 20 or h < 20:
            return
        image = self.source.copy()
        image.thumbnail((w, h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.configure(image=self.photo)


# ---------------------------
# OVERVIEW TAB
# ---------------------------

class OverviewTab(ttk.Frame):
    TILES = [  # (name, value, note) from a summary()
        ("Games", lambda s: fmt_num(s["games"]), lambda s: f"{s['ranked']} ranked"),
        ("Wins", lambda s: fmt_num(s["wins"]), lambda s: f"{fmt_pct(s['win_rate'])} win rate"),
        ("Top 5", lambda s: fmt_pct(s["top5"]), lambda s: "of games"),
        ("Avg placement", lambda s: fmt_num(s["placement"], 1), lambda s: "lower is better"),
        ("Kills / game", lambda s: fmt_num(s["kills"], 2), lambda s: f"{fmt_num(s['total_kills'])} total"),
        ("Assists / game", lambda s: fmt_num(s["assists"], 2), lambda s: ""),
        ("Knocks / game", lambda s: fmt_num(s["knocks"], 2), lambda s: ""),
        ("Damage / game", lambda s: fmt_num(s["damage"]), lambda s: f"{fmt_num(s['total_damage'])} total"),
        ("Squad kills / game", lambda s: fmt_num(s["squad_kills"], 1), lambda s: "whole squad"),
        ("Survival", lambda s: fmt_time(s["survival"]), lambda s: "average per game"),
        ("Revives / game", lambda s: fmt_num(s["revives"], 2), lambda s: f"respawns {fmt_num(s['respawns'], 2)}"),
        ("Share of squad damage", lambda s: fmt_pct(s["damage_share"]), lambda s: "captured games"),
        ("Ranked RP", lambda s: f"{s['rp']:+,.0f}", lambda s: f"{fmt_num(s['rp_per_game'], 1)} per ranked game"),
        ("Hot drops", lambda s: fmt_pct(s["contested"]), lambda s: "landings contested"),
        ("Avg lobby skill", lambda s: fmt_num(s["skill"], 1),
         lambda s: f"your team's percentile {fmt_num(s['team_percentile'])}" if s["team_percentile"] is not None else "Trios / Duos"),
        ("Top 3", lambda s: fmt_pct(s["top3"]), lambda s: "of games"),
        ("Kill participation", lambda s: fmt_pct(s["kp"]), lambda s: "(kills + assists) / squad kills"),
        # movement: captured games with map positions (Movement.py)
        ("On foot / game", lambda s: fmt_km(s["walked"]),
         lambda s: f"{fmt_num(s['pace'])} m a minute moving" if s["pace"] is not None else "captured games"),
        ("Squad spread", lambda s: fmt_m(s["spread"]), lambda s: "to your nearest teammate (median)"),
        ("Near a teammate", lambda s: fmt_pct(s["together"]),
         lambda s: f"of the time, within {Movement.TOGETHER_M} m" + (f" · alone {fmt_pct(s['alone'])}" if s["alone"] else "")),
        ("Your last fight", lambda s: fmt_m(s["death_gap"]),
         lambda s: f"nearest teammate · last up {fmt_pct(s['last_up'])}" if s["last_up"] is not None else "nearest teammate"),
        ("Glide", lambda s: fmt_m(s["glide"]), lambda s: "jump to landing, average"),
    ]
    BEST = [("Best damage", "damage"), ("Most kills", "kills")]    # single-game bests: the game's value
    BEST_AT = 16                # where they go among the tiles: the end of the third row, movement under them

    def __init__(self, master):
        super().__init__(master, padding=10)
        tiles = ttk.Frame(self)
        tiles.pack(fill="x")
        self.tiles, self.best_tiles = [], []
        names = [t[0] for t in self.TILES]
        names[self.BEST_AT:self.BEST_AT] = [name for name, _ in self.BEST]
        for i, name in enumerate(names):
            box = ttk.Frame(tiles, style="Tile.TFrame", padding=(10, 6))
            box.grid(row=i // 6, column=i % 6, sticky="nsew", padx=3, pady=3)
            ttk.Label(box, text=name, style="TileName.TLabel").pack(anchor="w")
            value = ttk.Label(box, text="–", style="TileValue.TLabel")
            value.pack(anchor="w")
            note = ttk.Label(box, text="", style="TileNote.TLabel")
            note.pack(anchor="w")
            best = self.BEST_AT <= i < self.BEST_AT + len(self.BEST)
            (self.best_tiles if best else self.tiles).append((value, note))
        for c in range(6):
            tiles.columnconfigure(c, weight=1, uniform="tile")

        breakdown = ttk.LabelFrame(self, text="Breakdown", padding=6)
        breakdown.pack(fill="both", expand=True, pady=(10, 0))
        top = ttk.Frame(breakdown)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Group by:").pack(side="left")
        self.group = ttk.Combobox(top, values=list(GROUPS), state="readonly", width=26)
        self.group.set("Legend")
        self.group.pack(side="left", padx=6)
        self.group.bind("<<ComboboxSelected>>", lambda e: self.show_breakdown())
        ttk.Label(top, text="click a heading to sort", foreground=FAINT).pack(side="right")
        self.table = Table(breakdown, [
            ("name", "Group", 200, text, "w"), ("games", "Games", 70, fmt_num, "e"),
            ("wins", "Wins", 60, fmt_num, "e"), ("win_rate", "Win %", 65, fmt_pct, "e"),
            ("top5", "Top 5 %", 70, fmt_pct, "e"), ("placement", "Avg place", 80, lambda v: fmt_num(v, 1), "e"),
            ("kills", "Kills", 60, lambda v: fmt_num(v, 2), "e"), ("knocks", "Knocks", 65, lambda v: fmt_num(v, 2), "e"),
            ("damage", "Damage", 75, fmt_num, "e"), ("kp", "Kill part.", 75, fmt_pct, "e"),
            ("survival", "Survival", 75, fmt_time, "e"),
            ("spread", "Spread", 65, lambda v: fmt_num(v), "e"), ("together", "Near squad", 85, fmt_pct, "e"),
            ("rp_per_game", "RP / ranked", 90, lambda v: fmt_num(v, 1), "e")])
        self.table.pack(fill="both", expand=True)
        self.games = []

    def show(self, games):
        self.games = games
        s = summary(games)
        for (value, note), (_, get_value, get_note) in zip(self.tiles, self.TILES):
            value.configure(text=get_value(s) if games else "–")
            note.configure(text=get_note(s) if games else "")
        for (value, note), (_, key) in zip(self.best_tiles, self.BEST):
            best = max(games, key=lambda g: g[key] or 0, default=None)
            value.configure(text=fmt_num(best[key]) if best else "–")
            note.configure(text=f"{best['legend']} · {best['map']} · {best['time']:%d %b}" if best else "")
        self.show_breakdown()

    def show_breakdown(self):
        groups = defaultdict(list)
        for g in self.games:
            for name in GROUPS[self.group.get()](g):
                groups[name or "(blank)"].append(g)
        rows = [dict(name=name, **summary(games)) for name, games in groups.items()]
        self.table.show(rows, sort_key=self.table.sort_key or "games", reverse=self.table.reverse or not self.table.sort_key)


# ---------------------------
# SQUAD TAB
# ---------------------------

def squad_summary(rows):
    """Per player-game averages for Squad Stats rows (placement / wins counted once per match)."""
    n = len(rows)
    matches = list({p["Match"]: p for p in rows}.values())
    m = len(matches)
    known_contest = [p for p in matches if p["Contested"] in ("Yes", "No")]
    return dict(
        games=n, matches=m,
        win_rate=sum(p["Placement"] == 1 for p in matches) / m if m else None,
        top5=sum((p["Placement"] or 99) <= 5 for p in matches) / m if m else None,
        placement=mean(p["Placement"] for p in matches),
        kills=mean(p["Kills"] for p in rows), total_kills=sum(p["Kills"] or 0 for p in rows),
        assists=mean(p["Assists"] for p in rows), knocks=mean(p["Knocks"] for p in rows),
        damage=mean(p["Damage"] for p in rows), total_damage=sum(p["Damage"] or 0 for p in rows),
        damage_share=mean(p["Damage %"] for p in rows),
        top=sum(p["top"] for p in rows) / n if n else None,
        survival=mean(p["Survival Time"] for p in rows),
        revives=mean(p["Revives"] for p in rows), respawns=mean(p["Respawns"] for p in rows),
        contested=sum(p["Contested"] == "Yes" for p in known_contest) / len(known_contest) if known_contest else None,
        kp=mean(p.get("kp") for p in rows),
        # movement (captured games with map positions, Movement.py)
        walked=mean(p.get("walked") for p in rows), pace=mean(p.get("pace") for p in rows),
        spread=middle(p.get("spread") for p in rows), together=mean(p.get("together") for p in rows),
        gap_me=middle(p.get("gap_me") for p in rows), near_me=mean(p.get("near_me") for p in rows),
    )


# what the Squad tab's breakdown can group players by (one Squad Stats row can fall in several groups)
SQUAD_GROUPS = {
    "Player": lambda p: [p["who"]],
    "You / friends / randoms": lambda p: ["You" if p["who"] == "You" else p["who"] if p["friend"] else "(randoms)"],
    "Played with": lambda p: p["with"] or ["(solo)"],
    "Legend": lambda p: [p["Legend"]],
    "Squad (all 3 legends)": lambda p: [" + ".join(l for l in (p["Squad"] or "").split("-") if l) or None],
    "Map": lambda p: [p["Map"]],
    "Game mode": lambda p: [p["Game Mode"]],
    "Landing (POI)": lambda p: [p["POI"]],
    "Own death zone": lambda p: [p["Death Zone"]],
    "Hot drop": lambda p: [p["Contested"]],
    "Distance from you (10s of m)": lambda p: [band(p.get("gap_me"), 10, 100, " m") if p["who"] != "You" else "You"],
    "Slot": lambda p: [fmt_num(p["Slot"]) if p["Slot"] else None],
    "Weekday": lambda p: [p["Time"].strftime("%A")],
    "Hour of day": lambda p: [p["Time"].strftime("%H:00")],
}
WHO = ["Everyone", "You", "Teammates", "Friends", "Randoms"]


class SquadTab(OverviewTab):
    """The Overview again, for every player in the squad (the workbook's Squad Stats: captured games only)."""
    TILES = [  # (name, value, note) from a squad_summary()
        ("Player-games", lambda s: fmt_num(s["games"]), lambda s: f"{s['matches']} matches"),
        ("Kills / game", lambda s: fmt_num(s["kills"], 2), lambda s: f"{fmt_num(s['total_kills'])} total"),
        ("Assists / game", lambda s: fmt_num(s["assists"], 2), lambda s: ""),
        ("Knocks / game", lambda s: fmt_num(s["knocks"], 2), lambda s: ""),
        ("Damage / game", lambda s: fmt_num(s["damage"]), lambda s: f"{fmt_num(s['total_damage'])} total"),
        ("Share of squad damage", lambda s: fmt_pct(s["damage_share"]), lambda s: "average"),
        ("Top damage in squad", lambda s: fmt_pct(s["top"]), lambda s: "of their games"),
        ("Survival", lambda s: fmt_time(s["survival"]), lambda s: "average per game"),
        ("Revives / game", lambda s: fmt_num(s["revives"], 2), lambda s: f"respawns {fmt_num(s['respawns'], 2)}"),
        ("Avg placement", lambda s: fmt_num(s["placement"], 1), lambda s: f"{fmt_pct(s['win_rate'])} wins"),
        ("Top 5", lambda s: fmt_pct(s["top5"]), lambda s: "of matches"),
        ("Hot drops", lambda s: fmt_pct(s["contested"]), lambda s: "landings contested"),
    ]
    # filled in by show(): squad totals, teammates, single-game bests
    EXTRA = ["Squad damage / match", "Squad kills / match", "Most played with", "Best teammate",
             "Best damage", "Most kills"]
    NAME_TILES = {"Most played with", "Best teammate"}
    # a fourth row, from a squad_summary() like TILES: movement (captured games with map positions, Movement.py)
    MOVE_TILES = [
        ("Kill participation", lambda s: fmt_pct(s["kp"]), lambda s: "(kills + assists) / squad kills"),
        ("On foot / game", lambda s: fmt_km(s["walked"]), lambda s: "after landing"),
        ("Pace", lambda s: f"{fmt_num(s['pace'])} m/min" if s["pace"] is not None else "–", lambda s: "while seen on foot"),
        ("Squad spread", lambda s: fmt_m(s["spread"]), lambda s: "to their nearest squadmate (median)"),
        ("Distance from you", lambda s: fmt_m(s["gap_me"]), lambda s: "teammates: median, while both seen"),
        ("Near you", lambda s: fmt_pct(s["near_me"]), lambda s: f"teammates: of the time, within {Movement.TOGETHER_M} m"),
    ]

    def __init__(self, master):
        ttk.Frame.__init__(self, master, padding=10)
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Players:").pack(side="left")
        self.who = tk.StringVar(value=WHO[0])
        for name in WHO:
            ttk.Radiobutton(top, text=name, value=name, variable=self.who, style="Toolbutton",
                            command=self.show_rows).pack(side="left", padx=(4, 0))
        ttk.Label(top, text="Squad Stats: one row per player per captured match", foreground=FAINT).pack(side="right")

        tiles = ttk.Frame(self)
        tiles.pack(fill="x")
        self.tiles = []
        for i, name in enumerate([t[0] for t in self.TILES] + self.EXTRA + [t[0] for t in self.MOVE_TILES]):
            box = ttk.Frame(tiles, style="Tile.TFrame", padding=(10, 6))
            box.grid(row=i // 6, column=i % 6, sticky="nsew", padx=3, pady=3)
            ttk.Label(box, text=name, style="TileName.TLabel").pack(anchor="w")
            # a player's name is longer than a number: smaller, so it fits the tile
            value = ttk.Label(box, text="–", style="TileText.TLabel" if name in self.NAME_TILES else "TileValue.TLabel")
            value.pack(anchor="w", fill="y", expand=True)
            note = ttk.Label(box, text="", style="TileNote.TLabel")
            note.pack(anchor="w")
            self.tiles.append((value, note))
        for c in range(6):
            tiles.columnconfigure(c, weight=1, uniform="tile")

        breakdown = ttk.LabelFrame(self, text="Breakdown", padding=6)
        breakdown.pack(fill="both", expand=True, pady=(10, 0))
        top = ttk.Frame(breakdown)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Group by:").pack(side="left")
        self.group = ttk.Combobox(top, values=list(SQUAD_GROUPS), state="readonly", width=24)
        self.group.set("Player")
        self.group.pack(side="left", padx=6)
        self.group.bind("<<ComboboxSelected>>", lambda e: self.show_breakdown())
        ttk.Label(top, text="at least").pack(side="left", padx=(12, 4))
        self.min_games = tk.IntVar(value=2)
        ttk.Spinbox(top, from_=1, to=100, width=4, textvariable=self.min_games, state="readonly",
                    command=self.show_breakdown).pack(side="left")
        ttk.Label(top, text="games").pack(side="left", padx=4)
        ttk.Label(top, text="click a heading to sort", foreground=FAINT).pack(side="right")
        self.table = Table(breakdown, [
            ("name", "Group", 190, text, "w"), ("games", "Games", 60, fmt_num, "e"),
            ("win_rate", "Win %", 60, fmt_pct, "e"), ("placement", "Avg place", 75, lambda v: fmt_num(v, 1), "e"),
            ("kills", "Kills", 55, lambda v: fmt_num(v, 2), "e"), ("assists", "Assists", 60, lambda v: fmt_num(v, 2), "e"),
            ("knocks", "Knocks", 60, lambda v: fmt_num(v, 2), "e"), ("damage", "Damage", 70, fmt_num, "e"),
            ("damage_share", "Dmg %", 60, fmt_pct, "e"), ("top", "Top dmg", 65, fmt_pct, "e"),
            ("survival", "Survival", 70, fmt_time, "e"), ("revives", "Revives", 65, lambda v: fmt_num(v, 2), "e"),
            ("respawns", "Respawns", 70, lambda v: fmt_num(v, 2), "e"), ("kp", "Kill part.", 70, fmt_pct, "e"),
            ("walked", "On foot", 65, fmt_num, "e"), ("gap_me", "From you", 70, fmt_num, "e"),
            ("near_me", "Near you", 70, fmt_pct, "e")])
        self.table.pack(fill="both", expand=True)
        self.games, self.rows = [], []

    def show(self, games):
        self.games = games
        self.show_rows()

    def chosen_rows(self):
        rows = [p for g in self.games for p in g["squad"]]
        who = self.who.get()
        if who == "You":
            return [p for p in rows if p["who"] == "You"]
        if who == "Teammates":
            return [p for p in rows if p["who"] != "You"]
        if who == "Friends":
            return [p for p in rows if p["friend"]]
        if who == "Randoms":
            return [p for p in rows if p["who"] != "You" and not p["friend"]]
        return rows

    def show_rows(self):
        self.rows = rows = self.chosen_rows()
        s = squad_summary(rows)
        moving = self.tiles[len(self.TILES) + len(self.EXTRA):]
        for (value, note), (_, get_value, get_note) in zip(self.tiles[:len(self.TILES)] + moving, self.TILES + self.MOVE_TILES):
            value.configure(text=get_value(s) if rows else "–")
            note.configure(text=get_note(s) if rows else "")
        extra = dict(zip(self.EXTRA, self.tiles[len(self.TILES):]))

        def tile(name, value="–", note=""):
            extra[name][0].configure(text=value)
            extra[name][1].configure(text=note)

        # Whole squads of the matches these players were in
        squads = [g["squad"] for g in self.games if any(p in rows for p in g["squad"])] if rows else []
        tile("Squad damage / match", fmt_num(mean(sum(p["Damage"] or 0 for p in q) for q in squads)) if squads else "–",
             f"{len(squads)} matches" if squads else "")
        tile("Squad kills / match", fmt_num(mean(q[0]["Squad Kills"] for q in squads), 1) if squads else "–",
             "from the stats screen" if squads else "")
        mates = defaultdict(list)       # just you: the teammates you had in those matches
        for p in [p for q in squads for p in q] if self.who.get() == "You" else rows:
            if p["who"] not in ("You", "(no name)"):
                mates[p["who"]].append(p)
        if mates:
            name, games = max(mates.items(), key=lambda m: len(m[1]))
            tile("Most played with", name, f"{len(games)} games")
            regulars = {n: g for n, g in mates.items() if len(g) >= 3} or mates
            name, games = max(regulars.items(), key=lambda m: mean(p["Damage"] for p in m[1]) or 0)
            tile("Best teammate", name, f"{fmt_num(mean(p['Damage'] for p in games))} damage / game · {len(games)} games")
        else:
            tile("Most played with"), tile("Best teammate")
        for name, key in (("Best damage", "Damage"), ("Most kills", "Kills")):
            best = max(rows, key=lambda p: p[key] or 0, default=None)
            if best:
                tile(name, fmt_num(best[key]), f"{best['who']} · {best['Legend'] or '?'} · {best['Time']:%d %b}")
            else:
                tile(name)
        self.show_breakdown()

    def show_breakdown(self):
        groups = defaultdict(list)
        for p in self.rows:
            for name in SQUAD_GROUPS[self.group.get()](p):
                groups[name or "(blank)"].append(p)
        least = self.min_games.get()
        rows = [dict(name=name, **squad_summary(rows)) for name, rows in groups.items() if len(rows) >= least]
        self.table.show(rows, sort_key=self.table.sort_key or "games", reverse=self.table.reverse or not self.table.sort_key)


# ---------------------------
# MATCHES TAB
# ---------------------------

def missing_legends(g):
    """A captured game with a legend nobody could read (no legend select seen)."""
    return bool(g["match"]) and (not g["legend"] or any(not p.get("Legend") for p in g["squad"]))


class LegendDialog(tk.Toplevel):
    """Type in who played what, looking at the stats screen (for games where legend select wasn't seen).
    Saved to Captures/<match>/fixes.json ("legends": {name on the stats screen: legend}), then
    `Sheet_Write.py --update` rewrites the match in the workbook."""

    def __init__(self, master, match, screen, on_saved):
        super().__init__(master, background=BG)
        self.match, self.on_saved = match, on_saved
        self.folder = CAPTURES / match
        stats = json.loads((self.folder / "stats.json").read_text())
        squad = Extract_Stats.build_rows(stats)[1]           # current legends, fixes included
        self.title(f"Legends · {match}")
        self.geometry("1320x700")
        self.transient(master.winfo_toplevel())

        side = ttk.Frame(self, padding=14)
        side.pack(side="right", fill="y")
        ttk.Label(side, text="Who played what", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        ttk.Label(side, text="Players as on the stats screen,\nleft to right.", foreground=MUTED,
                  justify="left").pack(anchor="w", pady=(2, 8))
        legends = [""] + sorted(Extract_Stats.LEGENDS)
        self.boxes = []
        for p in squad:
            name = p["Player"] or f"Player {p['Slot']} (name not read)"
            ttk.Label(side, text=name + ("  (you)" if p["Is Me"] == "Yes" else "")).pack(anchor="w", pady=(8, 2))
            box = ttk.Combobox(side, values=legends, state="readonly", width=22, height=len(legends))
            box.set(p["Legend"] or "")
            box.pack(anchor="w")
            self.boxes.append((p["Player"] or f"#{p['Slot']}", p["Legend"] or "", box))
        buttons = ttk.Frame(side)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", expand=True, fill="x")

        picture = FittedImage(self)
        picture.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        picture.set(screen, missing="No stats screen kept for this match")
        self.bind("<Escape>", lambda e: self.destroy())

    def save(self):
        chosen = {name: box.get() for name, read, box in self.boxes if name and box.get() and box.get() != read}
        path = self.folder / "fixes.json"
        fixes = json.loads(path.read_text()) if path.exists() else {}
        # Nothing changed, but legends were saved before: write the match again anyway. The boxes show the saved
        # legends, so a workbook write that didn't go through could never be redone (2026-10-31 18:07 stayed
        # "missing legends" with Fuse in its fixes.json)
        if not chosen and not fixes.get("legends"):
            self.destroy()
            return
        book = Seasons.workbook(Seasons.season_of_match(self.match))
        if (HERE / f".~lock.{book.name}#").exists():
            messagebox.showinfo("Workbook open", f"Close {book.name} in Calc first, then Save again.", parent=self)
            return
        if chosen:
            fixes["legends"] = {**fixes.get("legends", {}), **chosen}
            path.write_text(json.dumps(fixes, indent=2))
        self.destroy()
        self.on_saved(self.match)


class MatchesTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        self.only_missing = tk.BooleanVar(value=False)
        self.missing_button = ttk.Checkbutton(top, text="Missing legends only", variable=self.only_missing,
                                              style="Toolbutton", command=lambda: self.show(self.games))
        self.missing_button.pack(side="left")
        self.job_note = ttk.Label(top, text="", foreground=MUTED)
        self.job_note.pack(side="left", padx=12)
        ttk.Label(top, text="pick a match to see its squad", foreground=FAINT).pack(side="right")
        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True)
        self.table = Table(panes, [
            ("season", "Season", 55, lambda v: f"S{v}", "w"), ("time", "When", 130, fmt_when, "w"),
            ("mode", "Mode", 70, text, "w"), ("map", "Map", 100, text, "w"),
            ("placement", "Place", 55, fmt_num, "e"), ("kills", "Kills", 50, fmt_num, "e"),
            ("assists", "Assists", 60, fmt_num, "e"), ("knocks", "Knocks", 60, fmt_num, "e"),
            ("damage", "Damage", 70, fmt_num, "e"), ("damage_share", "Dmg %", 60, fmt_pct, "e"),
            ("survival", "Survival", 65, fmt_time, "e"), ("legend", "Legend", 95, text, "w"),
            ("mates_text", "Teammates", 150, text, "w"), ("friends", "Friends", 90, text, "w"),
            ("poi", "Landed", 120, text, "w"), ("contested", "Hot drop", 70, text, "w"),
            ("death_zone", "Died at", 120, text, "w"), ("rp", "RP", 55, lambda v: fmt_num(v), "e"),
            ("skill", "Skill", 55, lambda v: fmt_num(v, 1), "e")],
            height=14, on_select=self.pick)
        panes.add(self.table, weight=3)

        detail = ttk.Frame(panes, padding=(0, 8, 0, 0))
        panes.add(detail, weight=2)
        holder = ttk.Frame(detail, width=340)
        holder.pack(side="left", fill="y")
        holder.pack_propagate(False)
        self.picture = FittedImage(holder)
        self.picture.pack(fill="both", expand=True)
        right = ttk.Frame(detail, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self.heading = ttk.Label(right, text="Pick a match above to see its squad.", font=("TkDefaultFont", 11, "bold"))
        self.heading.pack(anchor="w")
        self.subheading = ttk.Label(right, text="", foreground=MUTED)
        self.subheading.pack(anchor="w", pady=(0, 6))
        self.squad = Table(right, [
            ("Player", "Player", 140, text, "w"), ("Legend", "Legend", 90, text, "w"),
            ("Rank", "Rank", 85, text, "w"), ("in_lobby", "In lobby", 165, text, "w"),
            ("Kills", "Kills", 50, fmt_num, "e"), ("Assists", "Assists", 60, fmt_num, "e"),
            ("Knocks", "Knocks", 60, fmt_num, "e"), ("Damage", "Damage", 70, fmt_num, "e"),
            ("Damage %", "Dmg %", 60, fmt_pct, "e"), ("Survival Time", "Survival", 70, fmt_time, "e"),
            ("Revives", "Revives", 60, fmt_num, "e"), ("Respawns", "Respawns", 70, fmt_num, "e"),
            ("kp", "Kill part.", 65, fmt_pct, "e"), ("walked", "On foot", 60, fmt_num, "e"),
            ("gap_me", "From you", 65, fmt_num, "e"), ("Death Zone", "Died at", 120, text, "w")], height=3)
        self.squad.pack(fill="both", expand=True)
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=(6, 0))
        self.buttons = [ttk.Button(buttons, text=label, state="disabled", command=lambda f=f: self.open(f))
                        for label, f in (("Match picture", "match_map.jpg"), ("Stats screen", "summary.jpg"),
                                         ("Match folder", ""))]
        for b in self.buttons:
            b.pack(side="left", padx=(0, 6))
        self.legend_button = ttk.Button(buttons, text="Set legends…", state="disabled", command=self.set_legends)
        self.legend_button.pack(side="left", padx=(12, 0))
        self.current, self.games = None, []
        self.job, self.job_output = None, queue.Queue()

    def show(self, games):
        self.games = games
        for g in games:
            g["mates_text"] = ", ".join(g["mates"])
        missing = sum(missing_legends(g) for g in games)
        self.missing_button.configure(text=f"Missing legends only ({missing})")
        self.table.show([g for g in games if missing_legends(g)] if self.only_missing.get() else games,
                        sort_key=self.table.sort_key or "time",
                        reverse=self.table.reverse if self.table.sort_key else True)

    def pick(self, game):
        self.current = game
        if not game:
            return
        self.heading.configure(text=f"#{fmt_num(game['placement'])} · {game['mode'] or ''} · {game['map'] or ''} · "
                                    f"{game['time']:%A %d %B %Y, %H:%M}")
        bits = [f"landed {game['poi']}" if game["poi"] else "", "hot drop" if game["contested"] == "Yes" else "",
                f"died at {game['death_zone']}" if game["death_zone"] else "",
                f"{fmt_num(game['rp'])} RP{'' if game.get('rp_real') is not None else ' (estimate)'}"
                if is_ranked(game) and game["rp"] is not None else "",
                f"lobby: {game['rank_distribution']}" if game["rank_distribution"] else "",
                f"lobby skill {fmt_num(game['skill'], 1)}" if game.get("skill") is not None else ""]
        bits.append(Movement.describe(game))       # (wins have no last fight: add_movement left it out)
        if not game["match"]:
            bits.append("typed in by hand: no capture for this match")
        elif game["match"] in Extract_Stats.full_games().values():
            bits.append(f"{game['map']}'s full game: every screenshot kept (Captures/full_games.json)")
        self.subheading.configure(text="  ·  ".join(b for b in bits if b))
        self.squad.show([dict(p, Player=(p.get("Player") or "?") + ("  (you)" if p.get("Is Me") == "Yes" else ""),
                              in_lobby=lobby_standing(p.get("Rank"), game["rank_distribution"]))
                         for p in game["squad"]])
        self.picture.set(self.match_file("match_map.jpg"),
                         missing="No match picture" if game["match"] else "Not captured")
        for b, name in zip(self.buttons, ("match_map.jpg", "summary.jpg", "")):
            b.configure(state="normal" if self.match_file(name) else "disabled")
        self.legend_button.configure(state="normal" if self.match_file("stats.json") else "disabled")

    def match_file(self, name):
        """A file of the picked match, also before its clean-up (when the picture is still a .png and
        the stats screen still has its screenshot name)."""
        if not (self.current and self.current["match"]):
            return None
        folder = CAPTURES / self.current["match"]
        candidates = [folder / name] if name else [folder]
        if name == "match_map.jpg":
            candidates.append(folder / "match_map.png")
        elif name == "summary.jpg":
            try:
                screen = (json.loads((folder / "stats.json").read_text()).get("summary") or {}).get("file")
                candidates += [folder / screen] if screen else []
            except (OSError, ValueError):
                pass
        return next((path for path in candidates if path.exists()), None)

    def open(self, name):
        path = self.match_file(name)
        if path:
            open_path(path)

    # --- legends typed in ---

    def set_legends(self):
        if self.current and self.current["match"]:
            LegendDialog(self, self.current["match"], self.match_file("summary.jpg"), self.write_match)

    def write_match(self, match):
        """Rewrite the corrected match in the workbook in the background; the window re-reads the workbook
        by itself once it's saved."""
        if self.job and self.job.is_alive():
            messagebox.showinfo("Busy", "Still writing the last correction, try again in a moment.")
            return
        self.job_note.configure(text=f"Writing {match} to the workbook…", foreground=MUTED)

        def work():
            p = subprocess.run([str(PYTHON), "Sheet_Write.py", "--update", match], cwd=HERE,
                               capture_output=True, text=True)
            self.job_output.put((p.returncode, (p.stdout + p.stderr).strip()))

        self.job = threading.Thread(target=work, daemon=True)
        self.job.start()
        self.after(500, self.poll_job)

    def poll_job(self):
        if self.job_output.empty():
            self.after(500, self.poll_job)
            return
        code, output = self.job_output.get()
        if code == 0:
            self.job_note.configure(text=output.splitlines()[-1].strip() if output else "Saved", foreground=MUTED)
        else:
            self.job_note.configure(text="Couldn't write to the workbook (fixes.json is saved)", foreground="#ff8a80")
            messagebox.showerror("Workbook not updated", output[-1500:] or f"exit {code}")


# ---------------------------
# MAPS TAB
# ---------------------------

class MapsTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        left = ttk.Frame(self)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Map", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.maps = tk.Listbox(left, width=22, height=12, exportselection=False, activestyle="none",
                               font=("TkDefaultFont", 11), background=SURFACE, foreground=TEXT_COLOUR,
                               selectbackground=SELECTED, selectforeground=TEXT_COLOUR, relief="flat",
                               highlightthickness=1, highlightcolor=BORDER, highlightbackground=BORDER)
        self.maps.pack(fill="y", expand=True, pady=(4, 0))
        self.maps.bind("<<ListboxSelect>>", lambda e: self.refresh())
        # The item layers: a button with each item's icon, apart from the map layers
        ttk.Label(left, text="Items", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(10, 4))
        items = ttk.Frame(left)
        items.pack(anchor="w")
        self.layers, self.icons = {}, []          # (file, part) -> on?
        for file, kind, label, about in ITEM_LAYERS:
            self.layers[file, "both"] = tk.BooleanVar(value=False)
            icon = np.full((30, 30, 3), [int(RAISED[i:i + 2], 16) for i in (5, 3, 1)], np.uint8)    # (BGR)
            Match_Map.draw_item(icon, (15, 15), kind, 12)
            self.icons.append(ImageTk.PhotoImage(Image.fromarray(icon[..., ::-1].copy())))
            button = ttk.Checkbutton(items, image=self.icons[-1], variable=self.layers[file, "both"], style="Toolbutton",
                                     command=self.refresh)
            button.pack(side="left", padx=(0, 4))
            Tooltip(button, about)

        right = ttk.Frame(self, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        # Two rows: the layers split into parts (a group's name before its buttons: "Heatmap: Heat Landings"), then the
        # whole layers - one row didn't fit the window's default width
        grouped, single = ttk.Frame(right), ttk.Frame(right)
        grouped.pack(fill="x")
        single.pack(fill="x", pady=(4, 0))
        ttk.Label(grouped, text="Show:", foreground=MUTED).pack(side="left", padx=(0, 6))
        group = None
        for file, part, in_group, label, about, on in MAP_LAYERS:
            self.layers[file, part] = tk.BooleanVar(value=on)
            row = grouped if in_group else single
            if in_group and in_group != group:
                ttk.Label(row, text=f"{in_group}:", foreground=MUTED).pack(side="left", padx=(8, 3))
            group = in_group
            button = ttk.Checkbutton(row, text=label, variable=self.layers[file, part], style="Toolbutton",
                                     command=self.refresh)
            button.pack(side="left", padx=(0, 3))
            Tooltip(button, about)                 # (what it shows: on hover, so the map keeps its room)
        self.poi_names = tk.BooleanVar(value=True)
        ttk.Checkbutton(single, text="POI names", variable=self.poi_names, style="Toolbutton",
                        command=self.refresh).pack(side="left", padx=(0, 4))
        ttk.Button(single, text="Open in browser", command=self.open_svg).pack(side="right")
        self.about = ttk.Label(right, text="", foreground=MUTED, wraplength=700, justify="left")
        self.about.pack(fill="x", pady=(6, 0))
        self.stats = ttk.Label(right, text="", font=("TkDefaultFont", 10, "bold"), wraplength=700, justify="left")
        self.stats.pack(fill="x", pady=(2, 6))
        self.picture = MapView(right, dim=MAP_DIM)
        self.picture.pack(fill="both", expand=True)
        self.games, self.shown, self.scene_map = [], None, None

    def show(self, games):
        """games: filtered by everything except the map (this tab picks the map itself)."""
        self.games = games
        names = sorted(p.name for p in MAPS.iterdir() if p.is_dir()) if MAPS.is_dir() else []
        counts = Counter(g["map"] for g in games)
        chosen = self.chosen()
        self.map_names = names
        self.maps.delete(0, "end")
        for name in names:
            self.maps.insert("end", f"{name}  ({counts.get(name, 0)})")
        if names:
            index = names.index(chosen) if chosen in names else max(range(len(names)), key=lambda i: counts.get(names[i], 0))
            self.maps.selection_set(index)
        self.refresh()

    def chosen(self):
        picked = self.maps.curselection()
        return self.map_names[picked[0]] if picked and getattr(self, "map_names", None) else None

    def refresh(self, force=True):
        name = self.chosen()
        if not name:
            return
        layers = [((file, part), f"{group} {label.lower()}" if group else label, about)
                  for file, part, group, label, about, _ in MAP_LAYERS] + \
            [((file, "both"), label, about) for file, _, label, about in ITEM_LAYERS]
        on = [choice for choice, *_ in layers if self.layers[choice].get()]
        games = [g for g in self.games if g["map"] == name]
        if games:
            s = summary(games)
            landings = Counter(g["poi"] for g in games if g["poi"]).most_common(3)
            self.stats.configure(text=f"{name}: {s['games']} games · {s['wins']} wins ({fmt_pct(s['win_rate'])}) · "
                                      f"avg place {fmt_num(s['placement'], 1)} · {fmt_num(s['damage'])} damage / game · "
                                      f"{fmt_num(s['kills'], 2)} kills / game"
                                      + (" · most landed: " + ", ".join(f"{p} ({n})" for p, n in landings) if landings else ""))
        else:
            self.stats.configure(text=f"{name}: no games with these filters")
        folder = MAPS / name
        # Remade when the map, the layers picked or any of their files change (heatmaps are redrawn after every match)
        key = (name, tuple(on), self.poi_names.get(),
               tuple((folder / f).stat().st_mtime if (folder / f).exists() else None for f, _ in on))
        if not force and key == self.shown:
            return
        self.shown = key
        missing = [label for (file, part), label, _ in layers if (file, part) in on and not (folder / file).exists()]
        try:
            scene = self.scene(folder, on)
        except OSError:                    # a layer half-written: the next poll tries again
            self.shown = None
            return
        note = f"No {', '.join(missing)} for {name} yet: drawn after a captured match on this map." if missing else None
        self.about.configure(text="Just the map: pick layers to show on it (items: the icons under the map list)." if not on
                             else "Hover over a button for what it shows.")
        # The same map again (a layer turned on, or redrawn after a match): stay where you'd zoomed to
        self.picture.set(scene, keep_view=name == self.scene_map, missing=f"No map picture for {name}.")
        self.scene_map = name
        self.about.configure(text=self.about.cget("text") + "  Wheel: zoom · drag: move · double-click: whole map." +
                             ("\n" + note if note else ""))

    def scene(self, folder, on):
        """(map picture, layer pictures, marks, map size) for the MapView; None if there's no map picture."""
        base = next((folder / f for f in ("reference_clean.png", "reference.png") if (folder / f).exists()), None)
        if not base:
            return None
        picture = cv2.imread(str(base))
        pictures, marks, loaded = [], [], {}
        for f, part in on:                 # (file, "picture" / "marks" / "both"), bottom to top
            if not (folder / f).exists():
                continue
            if f not in loaded:
                loaded[f] = Map_View.load_layer(folder / f)
            layer, layer_marks = loaded[f]
            if part in ("picture", "both"):
                pictures.append(layer)
            if part in ("marks", "both"):
                marks += layer_marks
        if self.poi_names.get() and (folder / "pois.json").exists():
            marks += Map_View.poi_marks(json.loads((folder / "pois.json").read_text())["pois"])
        return picture, pictures, marks, picture.shape[1::-1]

    def open_svg(self):
        """The map as shown (layers picked, POI names) as one SVG, opened in the browser to zoom into."""
        name = self.chosen()
        scene = self.picture.scene
        if not name or not scene:
            return
        folder = MAPS / name
        base = next(folder / f for f in ("reference_clean.png", "reference.png") if (folder / f).exists())
        order = [(f, part) for f, part, *_ in MAP_LAYERS] + [(f, "both") for f, *_ in ITEM_LAYERS]   # bottom to top
        layers = [(folder / f, 1.0) for f, part in order
                  if self.layers[f, part].get() and part in ("picture", "both") and (folder / f).exists()]
        LAYER_VIEWS.mkdir(parents=True, exist_ok=True)
        out = Map_View.write_svg(LAYER_VIEWS / f"{name}.svg", scene[3], [(base, 1.0)] + layers, scene[2], name,
                                 dim=MAP_DIM, embed=True)
        open_path(out)

    def poll(self):
        """Layers are redrawn after every match: pick up a new one."""
        self.refresh(force=False)


# ---------------------------
# CHARTS TAB
# ---------------------------

# Chart colours: the dark steps of a colour-blind-checked palette. One series is always BAR; RP gained / lost are the
# two ends of a diverging pair (blue / red), never a status green / red
BAR = "#3987e5"
BAR_FEW = "#4a505d"             # Games: a group under the "at least N games" (its stats aren't drawn: too few to go by)
LOSS = "#e66767"
DOTS = "#5d6472"                # single games behind a trend line
GRID = "#2a2e37"

# What a chart can show of a group of games: key in summary() -> (title, short name for the Show boxes, format,
# lower is better (None: neither - more isn't better or worse, so no "best"), the value of one game for Over time -
# None: only for groups)
METRICS = {
    "games": ("Games", "Games", "num0", False, None),
    "damage": ("Avg damage", "Damage", "num0", False, lambda g: g["damage"]),
    "placement": ("Avg placement", "Placement", "num1", True, lambda g: g["placement"]),
    "kills": ("Kills / game", "Kills", "num2", False, lambda g: g["kills"]),
    "knocks": ("Knocks / game", "Knocks", "num2", False, lambda g: g["knocks"]),
    "assists": ("Assists / game", "Assists", "num2", False, lambda g: g["assists"]),
    "squad_kills": ("Squad kills / game", "Squad kills", "num1", False, lambda g: g["squad_kills"]),
    "top5": ("Top 5", "Top 5", "pct", False, lambda g: float(g["placement"] <= 5) if g["placement"] else None),
    "win_rate": ("Win rate", "Wins", "pct", False, lambda g: float(g["placement"] == 1) if g["placement"] else None),
    "survival": ("Survival", "Survival", "time", False, lambda g: g["survival"]),
    "revives": ("Revives / game", "Revives", "num2", False, lambda g: g["revives"]),
    "damage_share": ("Share of squad damage", "Dmg share", "pct", False, lambda g: g["damage_share"]),
    "rp_per_game": ("RP / ranked game", "RP", "num1", False, lambda g: g["rp"] if is_ranked(g) else None),
    "skill": ("Lobby skill", "Skill", "num1", False, lambda g: g["skill"]),
    "kp": ("Kill participation", "Kill part.", "pct", False, lambda g: g["kp"]),
    "walked": ("On foot (m)", "On foot", "num0", None, lambda g: g["walked"]),
    "spread": ("Squad spread (m, median)", "Spread", "num0", None, lambda g: g["spread"]),
    "together": ("Time near a teammate", "Near squad", "pct", None, lambda g: g["together"]),
    "death_gap": ("Teammate in your last fight (m, median)", "Last fight", "num0", None, lambda g: g["death_gap"]),
}
FORMATS = {"num0": fmt_num, "num1": lambda v: fmt_num(v, 1), "num2": lambda v: fmt_num(v, 2), "pct": fmt_pct,
           "time": fmt_time}
SHOW_PER_LINE = 10              # Skill distribution's Show boxes a line
DISTRIBUTION_PANELS = ["games", "damage", "placement", "kills", "knocks", "squad_kills", "top5", "survival"]

# What Skill distribution can sort games by: name -> (field, group sizes - the first is the default, None = every
# value its own group -, kind: "pct" 0-100, "rank" (rank_value) or "number", what it is)
DISTRIBUTIONS = {
    "Lobby skill": ("skill", [5, 2.5, 10, 20], "pct",
                    "Trios / Duos lobbies: the skill graph's Skill, (2 x band start + your team + 4 x band end) / 7; "
                    "Ranked (S31 on): where your squad's marker sat on the lobby tier bar, 0-100"),
    "Your team's percentile": ("team_percentile", [5, 10, 20], "pct",
                               "Trios / Duos lobbies: where the skill graph's red YOUR TEAM line was, 0-100"),
    "Lobby rank (Ranked)": ("lobby_rank", [0.25, 0.5, 1], "rank",
                            "Ranked: the lobby's average rank, from its rank distribution (every squad once, each at the middle of its tier)"),
    "Your rank (Ranked)": ("my_rank", [0.25, 0.5, 1], "rank", "Ranked, S31 on: your rank badge in the lobby"),
    "Squad rank (Ranked)": ("squad_rank", [0.25, 0.5, 1], "rank",
                            "Ranked, S31 on: your squad's average rank (its badges in the lobby)"),
    "Entry cost (Ranked)": ("entry", [None], "number", "Ranked: the RP the match cost to enter"),
    "Survey scans": ("scans", [None], "number", "captured games: survey beacon scans seen on the full map"),
    "Glide distance": ("glide", [100, 50, 200], "number",
                       "captured games: metres from where you jumped off the dropship to where you landed"),
    "Squad spread": ("spread", [10, 5, 20], "number",
                     "captured games: the median distance to your nearest teammate after landing, in metres "
                     "(the moments you and one were both on the minimap)"),
    "Time near a teammate": ("together_pct", [10, 5, 20], "pct",
                             f"captured games: share of the game after landing your nearest teammate was within "
                             f"{Movement.TOGETHER_M} m"),
    "On foot": ("walked", [500, 250, 1000], "number",
                "captured games: metres walked / run after landing (not flying: jump towers, balloons, ziplines left "
                "out) - mostly how long you lived"),
    "Kill participation": ("kp_pct", [10, 5, 20], "pct",
                           "the squad's kills you had a hand in, (kills + assists) / squad kills, % (a game with no squad "
                           "kills has none)"),
    "Placement": ("placement", [None], "number", "where your squad placed"),
    "Squad kills": ("squad_kills", [None], "number", "your squad's kills"),
}
CHARTS = ["Skill distribution", "Compare groups", "Over time", "Ranked RP"]
TREND_WINDOWS = [10, 25, 50, 100, 250]     # Over time: the line is the average of the last N games
COMPARE_MOST = 30                           # Compare groups: bars shown at most (the table has them all)


def group_label(low, size, kind):
    if kind == "rank":
        first, last = rank_name(low), rank_name(low + (size or 0.25) - 0.25)
        return TIERS[min(int(low), len(TIERS) - 1)] if size == 1 else first if first == last else f"{first} – {last}"
    if size is None:
        return fmt_num(low)
    return f"{low:g}–{low + size:g}"


def distribution(games, field, size, kind):
    """The games grouped by one measure, in order: [(label, games)]. Groups of a size run unbroken from the
    lowest to the highest (an empty group is a gap in the chart, not left out)."""
    groups = defaultdict(list)
    for g in games:
        value = g.get(field)
        if not isinstance(value, (int, float)):
            continue
        if kind == "pct":
            value = min(max(value, 0), 99.999)          # 100 goes with 95-100
        groups[math.floor(value / size + 1e-9) * size if size else value].append(g)
    if not groups:
        return []
    lows = sorted(groups)
    if size:
        lows = [lows[0] + i * size for i in range(round((lows[-1] - lows[0]) / size) + 1)]
    return [(group_label(low, size, kind), groups.get(low, [])) for low in lows]


def axis_text(kind, value):
    if kind == "pct":
        return f"{value:.0%}"
    if kind == "time":
        return fmt_time(value)
    return f"{value:,.0f}" if abs(value) >= 10 else f"{value:g}"


def style_axes(ax, title, kind, axis="y"):
    """Quiet axes: a title top left, hairline grid along the values, no box."""
    ax.set_title(title, loc="left", fontsize=9, color=TEXT_COLOUR, pad=6)
    ax.set_facecolor(BG)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(side == ("bottom" if axis == "y" else "left"))
    ax.spines["bottom" if axis == "y" else "left"].set_color(BORDER)
    ax.tick_params(colors=FAINT, labelsize=7, length=0, pad=4)
    ax.grid(axis=axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    values = ax.yaxis if axis == "y" else ax.xaxis
    values.set_major_locator(MaxNLocator(4))
    values.set_major_formatter(FuncFormatter(lambda v, _: axis_text(kind, v)))


def minute_ticks(ax, axis="y"):
    """A time axis ticks on whole minutes (1, 2, 5, 10...), once its range is known."""
    top = (ax.get_ylim() if axis == "y" else ax.get_xlim())[1]
    step = next((m for m in (1, 2, 5, 10, 15, 30, 60) if top / (m * 60) <= 5), 120) * 60
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_locator(MultipleLocator(step))


class ChartHover:
    """The tooltip on a chart: over a bar's column (or row), or the nearest point of a line along x."""

    def __init__(self, canvas):
        self.canvas, self.targets, self.tip, self.dot, self.shown = canvas, [], None, None, None
        canvas.mpl_connect("motion_notify_event", self.move)
        canvas.mpl_connect("axes_leave_event", lambda e: self.hide())

    def clear(self):
        self.targets, self.tip, self.dot, self.shown = [], None, None, None

    def bars(self, ax, bars, texts, across=False):
        self.targets.append((ax, "barh" if across else "bar", list(zip(bars, texts))))

    def points(self, ax, xs, ys, texts):
        self.targets.append((ax, "points", (xs, ys, texts)))

    def find(self, event):
        for ax, kind, data in self.targets:
            if event.inaxes is not ax or event.xdata is None:
                continue
            if kind == "points":
                xs, ys, texts = data
                if not xs:
                    return None
                i = min(range(len(xs)), key=lambda k: abs(xs[k] - event.xdata))
                return ax, (xs[i], ys[i]), texts[i], True
            for bar, label in data:
                if kind == "bar" and bar.get_x() <= event.xdata <= bar.get_x() + bar.get_width():
                    return ax, (bar.get_x() + bar.get_width() / 2, bar.get_height()), label, False
                if kind == "barh" and bar.get_y() <= event.ydata <= bar.get_y() + bar.get_height():
                    return ax, (bar.get_width(), bar.get_y() + bar.get_height() / 2), label, False
        return None

    def move(self, event):
        found = self.find(event)
        if not found:
            return self.hide()
        ax, xy, label, point = found
        if (ax, label) == self.shown:
            return
        self.hide(draw=False)
        right = ax.transLimits.transform(xy)[0] > 0.6         # near the right edge: put the tip on the left
        self.tip = ax.annotate(label, xy, xytext=(-12 if right else 12, 10), textcoords="offset points",
                               ha="right" if right else "left", fontsize=8, color=TEXT_COLOUR, zorder=20,
                               bbox=dict(boxstyle="round,pad=0.5", fc=RAISED, ec=BORDER, lw=0.8))
        if point:
            self.dot, = ax.plot(*xy, "o", ms=7, color=BAR, mec=BG, mew=2, zorder=19)
        self.shown = (ax, label)
        self.canvas.draw_idle()

    def hide(self, draw=True):
        if not self.shown:
            return
        for artist in (self.tip, self.dot):
            if artist:
                artist.remove()
        self.tip = self.dot = self.shown = None
        if draw:
            self.canvas.draw_idle()


class ChartsTab(ttk.Frame):
    """Charts of the filtered games: Skill distribution (the games in groups of lobby skill, rank, ... with your
    averages for each, like the spreadsheet's dashboard), Compare groups (one stat by legend, map, POI...), Over
    time (a stat's running average) and Ranked RP."""

    def __init__(self, master):
        super().__init__(master, padding=10)
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Chart:").pack(side="left")
        self.chart = tk.StringVar(value=CHARTS[0])
        for name in CHARTS:
            ttk.Radiobutton(top, text=name, value=name, variable=self.chart, style="Toolbutton",
                            command=self.pick_chart).pack(side="left", padx=(4, 0))
        ttk.Label(top, text="point at a bar or line for its numbers", foreground=FAINT).pack(side="right")

        self.options = {}
        dist = self.options["Skill distribution"] = ttk.Frame(self)
        row = ttk.Frame(dist)
        row.pack(fill="x")
        ttk.Label(row, text="Group by:").pack(side="left")
        self.measure = ttk.Combobox(row, values=list(DISTRIBUTIONS), state="readonly", width=24)
        self.measure.set(next(iter(DISTRIBUTIONS)))
        self.measure.pack(side="left", padx=6)
        self.measure.bind("<<ComboboxSelected>>", lambda e: self.pick_measure())
        ttk.Label(row, text="in groups of").pack(side="left", padx=(8, 4))
        self.size = ttk.Combobox(row, state="readonly", width=6)
        self.size.pack(side="left")
        self.size.bind("<<ComboboxSelected>>", lambda e: self.draw())
        ttk.Label(row, text="at least").pack(side="left", padx=(12, 4))
        self.least = tk.IntVar(value=5)
        ttk.Spinbox(row, from_=1, to=100, width=4, textvariable=self.least, state="readonly",
                    command=self.draw).pack(side="left")
        ttk.Label(row, text="games (fewer: grey)").pack(side="left", padx=4)
        row = ttk.Frame(dist)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Show:").pack(side="left", anchor="n")
        boxes = ttk.Frame(row)
        boxes.pack(side="left")
        self.panels = {}
        for i, (key, (_, short, *_)) in enumerate(METRICS.items()):     # SHOW_PER_LINE a line, so they fit the window
            self.panels[key] = tk.BooleanVar(value=key in DISTRIBUTION_PANELS)
            ttk.Checkbutton(boxes, text=short, variable=self.panels[key], command=self.draw).grid(
                row=i // SHOW_PER_LINE, column=i % SHOW_PER_LINE, sticky="w", padx=(6, 0))

        compare = self.options["Compare groups"] = ttk.Frame(self)
        ttk.Label(compare, text="Group by:").pack(side="left")
        self.group = ttk.Combobox(compare, values=list(GROUPS), state="readonly", width=22)
        self.group.set("Legend")
        self.group.pack(side="left", padx=6)
        self.group.bind("<<ComboboxSelected>>", lambda e: self.draw())
        self.compare_metric = self.metric_box(compare, "damage", with_games=True)
        ttk.Label(compare, text="at least").pack(side="left", padx=(12, 4))
        self.compare_least = tk.IntVar(value=10)
        ttk.Spinbox(compare, from_=1, to=500, width=4, textvariable=self.compare_least, state="readonly",
                    command=self.draw).pack(side="left")
        ttk.Label(compare, text="games").pack(side="left", padx=4)

        trend = self.options["Over time"] = ttk.Frame(self)
        self.trend_metric = self.metric_box(trend, "damage")
        ttk.Label(trend, text="line = average of the last").pack(side="left", padx=(12, 4))
        self.window = ttk.Combobox(trend, values=TREND_WINDOWS, state="readonly", width=5)
        self.window.set(TREND_WINDOWS[1])
        self.window.pack(side="left")
        self.window.bind("<<ComboboxSelected>>", lambda e: self.draw())
        ttk.Label(trend, text="games").pack(side="left", padx=4)

        self.options["Ranked RP"] = ttk.Frame(self)
        self.note = ttk.Label(self, text="", foreground=FAINT)
        self.note.pack(side="bottom", fill="x", pady=(6, 0))

        self.panes = ttk.Panedwindow(self, orient="vertical")
        self.panes.pack(side="bottom", fill="both", expand=True, pady=(8, 0))
        plot = ttk.Frame(self.panes)
        self.figure = Figure(facecolor=BG, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot)
        self.canvas.get_tk_widget().configure(background=BG, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.hover = ChartHover(self.canvas)
        self.panes.add(plot, weight=3)
        self.table_box = ttk.Frame(self.panes)
        self.table = Table(self.table_box, [
            ("name", "Group", 170, text, "w"), ("games", "Games", 60, fmt_num, "e"),
            ("win_rate", "Win %", 55, fmt_pct, "e"), ("top5", "Top 5 %", 60, fmt_pct, "e"),
            ("placement", "Avg place", 70, lambda v: fmt_num(v, 1), "e"),
            ("damage", "Damage", 65, fmt_num, "e"), ("kills", "Kills", 50, lambda v: fmt_num(v, 2), "e"),
            ("knocks", "Knocks", 55, lambda v: fmt_num(v, 2), "e"), ("assists", "Assists", 55, lambda v: fmt_num(v, 2), "e"),
            ("squad_kills", "Sq. kills", 60, lambda v: fmt_num(v, 1), "e"),
            ("survival", "Survival", 65, fmt_time, "e"), ("revives", "Revives", 60, lambda v: fmt_num(v, 2), "e"),
            ("damage_share", "Dmg %", 55, fmt_pct, "e"), ("kp", "Kill part.", 65, fmt_pct, "e"),
            ("skill", "Skill", 50, lambda v: fmt_num(v, 1), "e"),
            ("spread", "Spread", 55, fmt_num, "e"), ("together", "Near squad", 75, fmt_pct, "e"),
            ("walked", "On foot", 60, fmt_num, "e"),
            ("rp_per_game", "RP / ranked", 80, lambda v: fmt_num(v, 1), "e")], height=5)
        self.table.pack(fill="both", expand=True)
        self.panes.add(self.table_box, weight=1)
        self.games, self.stale = [], True
        self.bind("<Map>", lambda e: self.stale and self.draw())
        self.pick_measure(draw=False)
        self.pick_chart(draw=False)

    def metric_box(self, master, default, with_games=False):
        ttk.Label(master, text="Stat:").pack(side="left", padx=(12, 0))
        names = {METRICS[k][0]: k for k in METRICS if with_games or METRICS[k][4]}
        box = ttk.Combobox(master, values=list(names), state="readonly", width=22)
        box.set(METRICS[default][0])
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self.draw())
        box.key = lambda: names[box.get()]
        return box

    def pick_measure(self, draw=True):
        sizes = DISTRIBUTIONS[self.measure.get()][1]
        self.size.configure(values=["each"] if sizes == [None] else [f"{s:g}" for s in sizes])
        self.size.set(self.size.cget("values")[0])
        self.size.configure(state="disabled" if sizes == [None] else "readonly")
        self.table.sort_key = None                      # in group order again
        if draw:
            self.draw()

    def pick_chart(self, draw=True):
        chart = self.chart.get()
        for name, frame in self.options.items():
            frame.pack_forget()
        self.options[chart].pack(fill="x", pady=(8, 0), after=self.winfo_children()[0])
        has_table = str(self.table_box) in map(str, self.panes.panes())
        if chart in ("Skill distribution", "Compare groups") and not has_table:
            self.panes.add(self.table_box, weight=1)
        elif chart not in ("Skill distribution", "Compare groups") and has_table:
            self.panes.forget(self.table_box)
        self.table.sort_key = None
        if draw:
            self.draw()

    def show(self, games):
        self.games, self.stale = games, True
        if self.winfo_ismapped():
            self.draw()

    def draw(self):
        """Only drawn while the tab is on screen (a workbook change redraws it next time it's opened)."""
        if not self.winfo_ismapped():
            self.stale = True
            return
        self.stale = False
        self.figure.clear()
        self.hover.clear()
        {"Skill distribution": self.draw_distribution, "Compare groups": self.draw_compare,
         "Over time": self.draw_trend, "Ranked RP": self.draw_rp}[self.chart.get()]()
        self.canvas.draw_idle()

    def empty(self, message):
        self.figure.text(0.5, 0.5, message, ha="center", va="center", color=FAINT, fontsize=10)
        self.note.configure(text="")

    def draw_distribution(self):
        name = self.measure.get()
        field, sizes, kind, about = DISTRIBUTIONS[name]
        size = None if sizes == [None] else float(self.size.get())
        groups = distribution(self.games, field, size, kind)
        rows = [dict(name=label, **summary(games)) for label, games in groups]
        self.table.show(rows)
        counted = sum(r["games"] for r in rows)
        if not counted:
            return self.empty(f"None of these {len(self.games)} games has a {name.lower()}")
        self.note.configure(text=f"{about} · {counted:,} of these {len(self.games):,} games have one · "
                                 f"groups under {self.least.get()} games: grey count, no bars (they're in the table)")
        shown = [k for k in METRICS if self.panels[k].get()] or ["games"]
        across = min(4, len(shown))
        down = math.ceil(len(shown) / across)
        least = self.least.get()
        labels = [r["name"] for r in rows]
        for i, key in enumerate(shown):
            title, _, form, lower, _ = METRICS[key]
            ax = self.figure.add_subplot(down, across, i + 1)
            style_axes(ax, title + (" (lower is better)" if lower else ""), form)
            # one game's stats would set the scale for the rest: groups with too few are left empty
            values = [r[key] if key == "games" or r["games"] >= least else None for r in rows]
            bars = ax.bar(range(len(rows)), [v or 0 for v in values], width=0.78,
                          color=[BAR if r["games"] >= least else BAR_FEW for r in rows])
            fmt = FORMATS[form]
            self.hover.bars(ax, bars, [f"{name} {r['name']}\n{title}: {fmt(r[key])}\n{r['games']} games"
                                       + ("" if r["games"] >= least or key == "games" else " (too few to draw)")
                                       for r in rows])
            # One number on the chart: the best group with enough games (none for a stat with no better way)
            sure = [(v, j) for j, v in enumerate(values) if v is not None]
            if sure and key != "games" and lower is not None:
                v, j = (min if lower else max)(sure)
                ax.annotate(fmt(v), (j, v), xytext=(0, 3), textcoords="offset points", ha="center",
                            fontsize=7, color=MUTED)
            ax.set_xticks(range(len(rows)))
            bottom_row = i >= len(shown) - across
            ax.set_xticklabels(labels if bottom_row else [], rotation=55 if len(rows) > 8 else 0,
                               ha="right" if len(rows) > 8 else "center")
            ax.margins(x=0.02, y=0.12)
            if form == "time":
                minute_ticks(ax)

    def draw_compare(self):
        key, least = self.compare_metric.key(), self.compare_least.get()
        title, _, form, lower, _ = METRICS[key]
        groups = defaultdict(list)
        for g in self.games:
            for name in GROUPS[self.group.get()](g):
                groups[name or "(blank)"].append(g)
        rows = [dict(name=name, **summary(games)) for name, games in groups.items()]
        self.table.show(rows, sort_key=self.table.sort_key or "games",
                        reverse=self.table.reverse or not self.table.sort_key)
        rows = [r for r in rows if r["games"] >= least and r[key] is not None]
        if not rows:
            return self.empty(f"No {self.group.get().lower()} has {least}+ games with a {title.lower()}")
        rows.sort(key=lambda r: r[key], reverse=not lower)
        cut = len(rows) > COMPARE_MOST
        rows = rows[:COMPARE_MOST]
        order = "highest first" if lower is None else "best first"
        self.note.configure(text=f"{title} by {self.group.get().lower()}, {order} · groups with {least}+ games"
                                 + (f" · the first {COMPARE_MOST} shown, the table has every group" if cut else ""))
        ax = self.figure.add_subplot(1, 1, 1)
        style_axes(ax, title + (" (lower is better)" if lower else ""), form, axis="x")
        fmt = FORMATS[form]
        bars = ax.barh(range(len(rows)), [r[key] for r in rows], height=0.72, color=BAR)
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([f"{r['name'][:30]}  ·  {r['games']:,}" for r in rows], fontsize=8, color=MUTED)
        ax.invert_yaxis()
        for i, r in enumerate(rows):
            ax.annotate(fmt(r[key]), (r[key], i), xytext=(4, 0), textcoords="offset points", va="center",
                        fontsize=7, color=MUTED)
        ax.margins(x=0.08)
        if form == "time":
            minute_ticks(ax, axis="x")
        self.hover.bars(ax, bars, [f"{r['name']}\n{title}: {fmt(r[key])}\n{r['games']} games · "
                                   f"{fmt_num(r['placement'], 1)} avg place" for r in rows], across=True)

    def draw_trend(self):
        key, window = self.trend_metric.key(), int(self.window.get())
        title, _, form, lower, value = METRICS[key]
        games = [g for g in reversed(self.games) if value(g) is not None]       # oldest first
        if not games:
            return self.empty(f"None of these games has a {title.lower()}")
        values = [float(value(g)) for g in games]
        running, total = [], 0.0
        for i, v in enumerate(values):
            total += v - (values[i - window] if i >= window else 0)
            running.append(total / min(i + 1, window))
        xs = list(range(len(games)))
        fmt = FORMATS[form]
        self.note.configure(text=f"{title}, one dot per game (oldest left), line = the average of the last {window} "
                                 f"· {len(games):,} games" + (" · lower is better" if lower else ""))
        ax = self.figure.add_subplot(1, 1, 1)
        style_axes(ax, f"{title}: average of the last {window} games", form)
        if form != "pct":                                   # a single game's win / top 5 is just 0 or 1
            ax.scatter(xs, values, s=5, color=DOTS, alpha=0.45, linewidths=0)
            ax.set_ylim(0, sorted(values)[int(len(values) * 0.99)] * 1.1 or 1)    # the odd huge game off the top
        ax.plot(xs, running, color=BAR, linewidth=2)
        if form == "time":
            minute_ticks(ax)
        self.seasons(ax, games)
        ax.xaxis.set_major_locator(MaxNLocator(8, integer=True))
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda x, _: f"{games[int(x)]['time']:%d %b %y}" if 0 <= x < len(games) else ""))
        ax.set_xlim(-1, len(games))
        self.hover.points(ax, xs, running, [f"{g['time']:%a %d %b %Y %H:%M} · S{g['season']}\n"
                                            f"this game: {fmt(v)}\nlast {min(i + 1, window)}: {fmt(r)}"
                                            for i, (g, v, r) in enumerate(zip(games, values, running))])

    def seasons(self, ax, games):
        """A hairline and the season's name where each season starts (games in order; a name too close to the last
        one is left off)."""
        named = -len(games)
        for i, g in enumerate(games):
            if i == 0 or g["season"] != games[i - 1]["season"]:
                ax.axvline(i - 0.5, color=BORDER, linewidth=0.8, zorder=0)
                if i - named < len(games) * 0.025:
                    continue
                named = i
                ax.annotate(f"S{g['season']}", (i - 0.5, 1), xycoords=("data", "axes fraction"), xytext=(4, -10),
                            textcoords="offset points", fontsize=7, color=FAINT)

    def draw_rp(self):
        games = [g for g in reversed(self.games) if is_ranked(g) and isinstance(g["rp"], (int, float))]
        if not games:
            return self.empty("No ranked games with RP in these filters")
        running, season_rp = [], {}
        for g in games:
            season_rp[g["season"]] = season_rp.get(g["season"], 0) + g["rp"]
            running.append(season_rp[g["season"]])
        xs = list(range(len(games)))
        self.note.configure(text=f"{len(games):,} ranked games, oldest left · RP adds up from each season's start · "
                                 + " · ".join(f"S{s} {rp:+,.0f}" for s, rp in season_rp.items()))
        total = self.figure.add_subplot(2, 1, 1)
        style_axes(total, "RP this season so far", "num0")
        for season in season_rp:                      # one line per season: it starts again at 0
            part = [i for i in xs if games[i]["season"] == season]
            total.plot(part, [running[i] for i in part], color=BAR, linewidth=2)
        total.axhline(0, color=BORDER, linewidth=0.8)
        each = self.figure.add_subplot(2, 1, 2, sharex=total)
        style_axes(each, "RP each game (blue gained, red lost)", "num0")
        bars = each.bar(xs, [g["rp"] for g in games], width=0.8,
                        color=[BAR if g["rp"] >= 0 else LOSS for g in games])
        each.axhline(0, color=BORDER, linewidth=0.8)
        for ax in (total, each):
            self.seasons(ax, games)
            ax.xaxis.set_major_locator(MaxNLocator(8, integer=True))
            ax.xaxis.set_major_formatter(FuncFormatter(
                lambda x, _: f"{games[int(x)]['time']:%d %b %y}" if 0 <= x < len(games) else ""))
        total.set_xlim(-1, len(games))
        texts = [f"{g['time']:%a %d %b %Y %H:%M} · S{g['season']}\n#{fmt_num(g['placement'])} · {fmt_num(g['kills'])} kills"
                 f" · {g['rp']:+,.0f} RP\nseason: {r:+,.0f} RP" for g, r in zip(games, running)]
        self.hover.points(total, xs, running, texts)
        self.hover.bars(each, bars, texts)


# ---------------------------
# LATEST MATCH TAB
# ---------------------------

# The newest screen in match.json, for the status line
SCREEN_NAMES = {"waiting": "in the lobby", "legend_select": "legend select", "your_squad": "squad intro",
                "dropship": "in the dropship", "in_game": "in game", "map": "in game (map open)",
                "spectate": "spectating a teammate", "after_squad": "squad out", "post_game": "stats screen"}
LIVE_FOR = 90                       # s since the last screenshot that still counts as live
FULL_SHOTS = Screen_Notes.FULL_SHOTS    # match.json events that are whole frames
# What the Latest Match tab can show: the live map, or a screen with what was read on it drawn over it
VIEWS = [("live", "Live map"), ("zoom", "Zoomed"), ("guess", "Endgame guess"), ("lobby", "Lobby"), ("legend_select", "Legend select"),
         ("dropship", "Dropship"), ("map", "Map shot"), ("summary", "Stats screen"), ("newest", "Newest screenshot")]
NOTES_FOLDER = CAPTURES / ".notes"      # those pictures, for the newest match only
# "Follow match": the view for the newest screen of the match (the map shot and newest screenshot are only by hand)
STAGE_VIEWS = {"waiting": "lobby", "legend_select": "legend_select", "your_squad": "legend_select",
               "dropship": "dropship", "in_game": "live", "map": "live", "spectate": "live", "after_squad": "live",
               "post_game": "summary"}
PICKS_SETTLE = 10                   # s to wait after the last legend select screenshot before reading it
SESSION_GAP = 90                    # min between games that ends a session (the Latest Match tab's session line)
GUESS_SHOW = 8                      # s Follow match shows the new endgame guess for when a ring is found, then the live map
# Mid-match, legend select is read in its own low-priority process: OCR shouldn't slow the game or the window
READ_PICKS = ("import json, sys; from pathlib import Path; import Extract_Stats; folder = Path(sys.argv[1]); "
              "events = json.loads((folder / 'match.json').read_text())['events']; "
              "print(json.dumps(Extract_Stats.read_picks_live(folder, events)))")
# The stats screen as the capture keeps it (post_game_last.jpg, rewritten every few seconds while it's up), read straight
# away for the squad table - the match's own read comes after its map positions, often minutes later
READ_SUMMARY = ("import json, sys, cv2; import Extract_Stats; image = cv2.imread(sys.argv[1]); "
                "print(json.dumps(Extract_Stats.parse_summary(image) if image is not None else None))")


def file_mtime(path):
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def seconds(value):
    """"13:07" (the stats screen's survival time) -> 787."""
    if isinstance(value, str) and re.fullmatch(r"\d+:\d\d", value):
        m, s = value.split(":")
        return int(m) * 60 + int(s)
    return None


class LatestTab(ttk.Frame):
    """The newest match, always (the filters don't apply): "Latest Match Map.png" big (redrawn every 5 s
    mid-match, the final picture once it's read), and underneath who's playing what from legend select, then
    everyone's stats once the match has been read."""

    def __init__(self, master):
        super().__init__(master, padding=10)
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=18, height=18, highlightthickness=0, background=BG)
        self.dot.pack(side="left")
        self.dot_item = self.dot.create_oval(3, 3, 15, 15, fill="grey", outline="")
        self.status = ttk.Label(top, text="Looking for the latest match...", font=("TkDefaultFont", 11, "bold"))
        self.status.pack(side="left", padx=(6, 0))
        self.hint = ttk.Label(top, text="", foreground=FAINT)
        self.hint.pack(side="right", padx=(12, 0))
        self.session = ttk.Label(self, text="", foreground=MUTED)
        self.session.pack(fill="x", pady=(4, 0))
        views = ttk.Frame(self)
        views.pack(fill="x", pady=(8, 0))
        self.view = tk.StringVar(value="live")
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(views, text="Follow match", variable=self.follow, style="Toolbutton",
                        command=lambda: self.refresh_map(force=True)).pack(side="left", padx=(0, 12))
        self.view_buttons = {}
        for view, label in VIEWS:
            self.view_buttons[view] = ttk.Radiobutton(views, text=label, value=view, variable=self.view, style="Toolbutton",
                                                      command=self.picked_view)
            self.view_buttons[view].pack(side="left", padx=(0, 4))

        info = ttk.Frame(self, padding=(0, 8, 0, 0))
        info.pack(side="bottom", fill="x")
        self.heading = ttk.Label(info, text="", font=("TkDefaultFont", 11, "bold"))
        self.heading.pack(anchor="w")
        self.subheading = ttk.Label(info, text="", foreground=MUTED)
        self.subheading.pack(anchor="w", pady=(0, 6))
        self.squad = Table(info, [
            ("Player", "Player", 160, text, "w"), ("Legend", "Legend", 100, text, "w"),
            ("Rank", "Rank", 85, text, "w"), ("in_lobby", "In lobby", 165, text, "w"),
            ("Kills", "Kills", 50, fmt_num, "e"), ("Assists", "Assists", 60, fmt_num, "e"),
            ("Knocks", "Knocks", 60, fmt_num, "e"), ("Damage", "Damage", 70, fmt_num, "e"),
            ("Damage %", "Dmg %", 60, fmt_pct, "e"), ("Survival Time", "Survival", 70, fmt_time, "e"),
            ("Revives", "Revives", 60, fmt_num, "e"), ("Respawns", "Respawns", 70, fmt_num, "e"),
            ("Death Zone", "Died at", 130, text, "w")], height=3)
        self.squad.pack(fill="x")
        self.picture = FittedImage(self)
        self.picture.pack(fill="both", expand=True, pady=(8, 0))

        self.match, self.games, self.map_mtime = None, [], None
        self.moved = (None, {})               # ((match, trails.json time), Movement numbers) for movement_line
        self.noting = None                      # (notes picture being made, its process): one at a time
        self.notes_failed = set()
        self.new_match(None)
        self.poll()

    def new_match(self, name):
        self.match = name
        self.log_mtime = self.stats_mtime = None
        self.events, self.over, self.started, self.stats = [], False, None, None
        self.picks, self.picks_read, self.reading = None, None, None   # legend select read mid-match
        self.screen_stats, self.screen_read, self.summing = None, None, None   # the stats screen, read straight away
        # Follow match's endgame guess on a new ring: rings in LIVE_RINGS last time, the guess being made for the
        # newest ones, and until when it's shown
        self.rings_seen, self.guess_due, self.guess_until = None, None, None
        if NOTES_FOLDER.is_dir():                # notes of older matches aren't looked at again
            for old in NOTES_FOLDER.iterdir():
                if old.name != name and MATCH_FOLDER.match(old.name):
                    shutil.rmtree(old, ignore_errors=True)

    def show(self, games):
        """All games (unfiltered): the workbook row gives the ranked RP once the match is in."""
        self.games = games
        self.show_session()
        self.show_match()

    def show_session(self):
        """This session at a glance: the games up to the newest with gaps under SESSION_GAP minutes."""
        session = []
        for g in self.games:                              # newest first
            if session and (session[-1]["time"] - g["time"]).total_seconds() > SESSION_GAP * 60:
                break
            session.append(g)
        if not session:
            self.session.configure(text="")
            return
        s = summary(session)
        recent = (datetime.now() - session[0]["time"]).total_seconds() < 3 * 3600
        bits = [f"{s['games']} game{'s' if s['games'] != 1 else ''}", f"{s['wins']} win{'s' if s['wins'] != 1 else ''}",
                f"{sum(g['placement'] <= 5 for g in session if g['placement'])} top 5",
                f"{fmt_num(s['damage'])} damage / game", f"{fmt_num(s['kills'], 1)} kills / game"]
        if s["ranked"]:
            bits.append(f"{s['rp']:+,.0f} RP")
        when = "This session" if recent else f"Last session ({session[-1]['time']:%a %d %b})"
        self.session.configure(text=f"{when}: " + " · ".join(bits))

    def poll(self):
        self.refresh_match()
        self.refresh_map()
        self.after(2000, self.poll)

    def newest_shot(self):
        """The match's newest whole screenshot (after the lobby) and what it is, or (None, "")."""
        for e in reversed(self.events):
            if e["event"] in FULL_SHOTS and e.get("file") and e["screen"] != "waiting":
                shot = CAPTURES / self.match / e["file"]
                if shot.exists():
                    return shot, f"{SCREEN_NAMES.get(e['screen'], e['screen'])} {e['time'][11:19]}"
        return None, ""

    def picture_now(self):
        """(picture, hint) for the chosen view. Live map: until it has drawn this match (it needs the dropship's
        minimaps), the newest screenshot instead. A screen: its screenshot with what was read drawn on it
        (Screen_Notes, made in the background the first time; the plain screenshot meanwhile)."""
        view = self.view.get()
        if view in ("live", "zoom"):
            path = LATEST_MAP if view == "live" else LATEST_ZOOM
            first_minimap = next((e["time"] for e in self.events if e["event"] == "minimap"), None)
            if first_minimap and (file_mtime(path) or 0) >= datetime.fromisoformat(first_minimap).timestamp():
                return path, "click the picture to open it full size"
            shot, what = self.newest_shot()
            if shot:
                return shot, f"newest screenshot ({what}) until the live map starts"
            return path, "click the picture to open it full size"     # still in the lobby: the last match's picture
        if view == "newest":
            shot, what = self.newest_shot()
            return shot, f"newest screenshot: {what}" if shot else "no screenshots kept for this match"
        if view == "guess":
            return self.guess_picture()
        source = self.screens().get(view) if self.match else None
        if not source:
            if view == "summary" and self.match and "summary" in Screen_Notes.sources(CAPTURES / self.match, self.events):
                return None, "waiting for the stats screen's placement and squad kills to come in"
            return None, "not kept for this match (screenshots go 20 min after the match is read)"
        # (named for the screenshot's time too: post_game_last.jpg is rewritten with later frames while it's up)
        notes = NOTES_FOLDER / self.match / f"{view}-{source.stem}-{int(file_mtime(source) or 0)}.jpg"
        if notes.exists():
            return notes, f"what was read on {source.name} · click to open full size"
        if notes in self.notes_failed:
            return source, f"couldn't work out what was read on {source.name} (plain screenshot)"
        map_name = (self.stats or {}).get("map") or (self.picks or {}).get("map") or ""
        self.make_notes(notes, ["Screen_Notes.py", str(CAPTURES / self.match), view, str(notes), str(source)]
                        + ([map_name] if map_name else []))
        return source, f"working out what was read on {source.name}..."

    def live_guess_source(self):
        """LIVE_RINGS if it's this match's (the live map writes it once it knows the map), else None."""
        try:
            live = json.loads(LIVE_RINGS.read_text())
            if self.started and datetime.fromisoformat(live["time"]) >= self.started:
                return live
        except (OSError, ValueError, KeyError):
            pass
        return None

    def follow_guess(self):
        """Follow match mid-match: a ring the live map hasn't had before (usually the next ring, once it's shown)
        -> make the endgame guess with it, show it GUESS_SHOW s once it's ready, then back to the live map.
        True while the guess should be up."""
        now = time.monotonic()
        if self.guess_until:
            if now < self.guess_until:
                return True
            self.guess_until = None
        live = self.live_guess_source()
        count = len(live["rings"]) if live else 0
        if self.rings_seen is None:
            self.rings_seen = count            # the window opened mid-match: only rings found from now on
        elif count > self.rings_seen:
            self.rings_seen = count
            self.guess_due = NOTES_FOLDER / self.match / f"guess-{zlib.crc32(LIVE_RINGS.read_bytes()):08x}.jpg"
        if self.guess_due:
            if self.guess_due.exists():
                self.guess_due, self.guess_until = None, now + GUESS_SHOW
                return True
            if self.guess_due in self.notes_failed:
                self.guess_due = None
            else:
                self.make_notes(self.guess_due, ["Ring_Predict.py", str(LIVE_RINGS), str(self.guess_due)])
        return False

    def guess_picture(self):
        """Ring_Predict's endgame guess for this match: from the live map's rings mid-match (LIVE_RINGS), from
        trails.json once the match is read. Remade when the rings change; the last one stays up meanwhile."""
        folder = CAPTURES / self.match
        source = folder / "trails.json" if (folder / "trails.json").exists() else None
        if not source and self.live_guess_source():
            source = LIVE_RINGS
        if not source:
            return None, "no guess yet: it starts once the live map knows which map this is"
        out = NOTES_FOLDER / self.match / f"guess-{zlib.crc32(source.read_bytes()):08x}.jpg"
        if self.guess_until:                      # Follow match's few seconds on the guess for a new ring
            newest = max((NOTES_FOLDER / self.match).glob("guess-*.jpg"), key=lambda p: p.stat().st_mtime, default=None)
            return newest, f"new ring: the endgame guess for {GUESS_SHOW} s, then back to the live map"
        if out.exists():
            return out, "endgame guess from the rings seen so far · click to open full size"
        older = sorted((NOTES_FOLDER / self.match).glob("guess-*.jpg"), key=lambda p: p.stat().st_mtime)
        if out not in self.notes_failed:
            self.make_notes(out, ["Ring_Predict.py", str(source), str(out)])
        return (older[-1] if older else None), "updating the endgame guess with the newest rings..."

    def make_notes(self, out, args):
        """Make one picture (Screen_Notes / Ring_Predict, `args` after the python) in a low-priority process."""
        if self.noting:
            done, process = self.noting
            if process.poll() is None:
                return                            # one at a time: the next refresh starts this one
            if not done.exists():
                self.notes_failed.add(done)       # don't keep trying a screenshot it can't read
            self.noting = None
            if out in self.notes_failed:
                return
        out.parent.mkdir(parents=True, exist_ok=True)
        self.noting = (out, subprocess.Popen(["nice", "-n", "15", str(PYTHON)] + args, cwd=HERE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def screens(self):
        """Screen_Notes.sources for this match, but the stats screen only once it's all there: the capture writes
        post_game_last.jpg as soon as the screen is made out, before its placement and squad kills have come in
        (2026-11-06 15:32:59), then again with later frames. Shown once the read straight off it has both (squad kills
        from their own box, not added up from the cards), once the screen has gone (a win has no "#n"; no later frame
        is coming), or once the match's own read has picked its screenshot."""
        have = Screen_Notes.sources(CAPTURES / self.match, self.events, self.stats)
        read = self.screen_stats or {}
        if "summary" in have and not ((self.stats or {}).get("summary") or {}).get("file") \
                and not ("placement" in read and "squad_kills" in read and not read.get("squad_kills_from_cards")) \
                and self.events and self.events[-1]["screen"] == "post_game" and self.events[-1]["event"] != "leave":
            del have["summary"]
        return have

    def picked_view(self):
        """A view picked by hand: stop following the match until Follow match is clicked again."""
        self.follow.set(False)
        self.refresh_map(force=True)

    def refresh_map(self, force=False):
        if self.match:
            have = self.screens()
            for view, button in self.view_buttons.items():
                button.configure(state="normal" if view in ("live", "zoom", "guess", "newest") or view in have else "disabled")
            stage = STAGE_VIEWS.get(self.events[-1]["screen"]) if self.events else None
            if self.follow.get() and stage == "live" and self.follow_guess():
                stage = "guess"
            if self.follow.get() and stage and (stage in ("live", "guess") or stage in have) and stage != self.view.get():
                self.view.set(stage)
                force = True
        path, hint = self.picture_now()
        key = (path, file_mtime(path))
        self.hint.configure(text=hint)
        if force or key != self.map_mtime:
            self.picture.set(path, missing="No match map yet: it's drawn once a match is under way."
                             if self.view.get() in ("live", "zoom") else "Nothing to show for this screen")
            self.map_mtime = key if self.picture.source or key[1] is None else None   # unreadable: try again

    def refresh_match(self):
        folders = match_folders()
        if not folders:
            return
        if folders[0] != self.match:
            self.new_match(folders[0])
        folder = CAPTURES / self.match
        mtime = file_mtime(folder / "match.json")
        if mtime != self.log_mtime:
            try:
                log = json.loads((folder / "match.json").read_text())
                self.events, self.over = log["events"], log["finished"]
                self.started = datetime.fromisoformat(log["started"])
                self.log_mtime = mtime
            except (OSError, ValueError, KeyError):
                pass
        # The stats read so far while the map positions are still being worked out (Extract_Stats.QUICK_STATS),
        # until the full stats.json is written (newer)
        name = max(("stats.json", Extract_Stats.QUICK_STATS), key=lambda n: file_mtime(folder / n) or 0)
        mtime = file_mtime(folder / name)
        if mtime != self.stats_mtime:
            try:
                self.stats = json.loads((folder / name).read_text())
                self.stats_mtime = mtime
            except (OSError, ValueError):
                pass
        self.read_picks(folder)
        self.read_summary(folder)
        self.show_match()

    def read_summary(self, folder):
        """The stats screen read as soon as the capture has kept it (and again each time it's rewritten with a later
        frame), until the match's own read has its stats: the squad table shows them meanwhile."""
        if self.summing:
            match, process, stamp = self.summing
            if process.poll() is None:
                return
            self.summing = None
            try:
                read = json.loads(process.communicate()[0])
                if match == self.match and read and read.get("players"):
                    self.screen_stats = read
            except ValueError:
                pass
        if self.stats and (self.stats.get("finished") or self.stats.get("quick")):
            return
        shot = folder / "post_game_last.jpg"
        stamp = file_mtime(shot)
        if stamp is None or stamp == self.screen_read:
            return
        self.screen_read = stamp
        self.summing = (self.match, subprocess.Popen(
            ["nice", "-n", "15", str(PYTHON), "-c", READ_SUMMARY, str(shot)], cwd=HERE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True), stamp)

    def read_picks(self, folder):
        """Before the match is read: who picked what, from the legend select screenshots so far (read again
        if a late pick reveal comes in)."""
        if self.reading:
            match, process = self.reading
            if process.poll() is None:
                return
            self.reading = None
            try:
                if match == self.match:
                    self.picks = json.loads(process.communicate()[0]) or self.picks
            except ValueError:
                pass
        if self.stats and "legend_select" in self.stats:
            return
        shots = [e for e in self.events if e.get("file")
                 and (e["screen"] == "legend_select" or e["event"] == "pick_reveal")]
        files = [e["file"] for e in shots]
        if not shots or files == self.picks_read:
            return
        if datetime.now() - max(datetime.fromisoformat(e["time"]) for e in shots) < timedelta(seconds=PICKS_SETTLE):
            return
        self.picks_read = files
        self.reading = (self.match, subprocess.Popen(
            ["nice", "-n", "15", str(PYTHON), "-c", READ_PICKS, str(folder)], cwd=HERE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))

    def movement_line(self, won=False):
        """How you moved this match (Movement.describe), once its read has made trails.json; worked out again only
        when trails.json changes. A win has no last fight (trails.json's death is just where you were)."""
        trails = CAPTURES / self.match / "trails.json"
        stamp = file_mtime(trails)
        if not stamp:
            return ""
        if self.moved[0] != (self.match, stamp):
            try:
                self.moved = ((self.match, stamp), Movement.match_movement(json.loads(trails.read_text())))
            except (OSError, ValueError, KeyError, TypeError):
                self.moved = ((self.match, stamp), {})
        m = dict(self.moved[1])
        if won:
            m["death_gap"] = m["last_up"] = None
        return Movement.describe(m)

    def show_match(self):
        if not self.match:
            self.status.configure(text="No matches captured yet")
            return
        stats = self.stats or {}
        picks = stats.get("legend_select") or self.picks or {}
        mode, map_name = stats.get("mode") or picks.get("mode"), stats.get("map") or picks.get("map")
        now = datetime.now()
        last = datetime.fromisoformat(self.events[-1]["time"]) if self.events else None
        jumped = next((datetime.fromisoformat(e["time"]) for e in self.events if e["screen"] == "in_game"), None)
        screen = SCREEN_NAMES.get(self.events[-1]["screen"], self.events[-1]["screen"]) if self.events else ""
        seen = f"{last or self.started:%H:%M}" if last or self.started else "?"
        if stats.get("quick"):
            state, colour = "Stats read · working out the map positions...", "#ffb000"
        elif self.screen_stats and not stats.get("finished"):
            state, colour = "Stats screen read · the match is read after it (map positions, workbook)...", "#ffb000"
        elif stats.get("finished"):
            state, colour = f"Finished {seen}", ACCENT
        elif self.over or (self.events and self.events[-1]["screen"] == "post_game"):
            state, colour = "Match over · reading the stats...", "#ffb000"
        elif last and now - last < timedelta(seconds=LIVE_FOR):
            since = f" · {fmt_time((now - jumped).total_seconds())} since the jump" if jumped else ""
            state, colour = f"Live · {screen}{since}", "#3cb44b"
        else:
            state, colour = f"Last seen {seen} ({screen or 'nothing saved'})", "grey"
        self.dot.itemconfigure(self.dot_item, fill=colour)
        self.status.configure(text=state)

        bits = []
        lobby = picks.get("rank_distribution", "")    # Ranked, mid-match (read_picks_live); the read's row wins
        # Before the match's own read: the stats screen as read straight off it, with the legend select picks
        early = self.screen_stats and not stats.get("finished") and not stats.get("quick")
        if early:
            stats = dict(stats, match=self.match, summary=self.screen_stats, finished=True,
                         **({"legend_select": picks} if picks else {}))
        if stats.get("finished"):
            try:
                row, squad = Extract_Stats.build_rows(stats)       # with fixes.json, like the workbook
            except (OSError, ValueError, KeyError):
                row, squad = None, []
            game = next((g for g in self.games if g["match"] == self.match), None)
            placement = row[0] if row else ""
            self.heading.configure(text=" · ".join(str(b) for b in (
                f"#{placement}" if placement else "", mode, map_name,
                f"{self.started:%A %d %B, %H:%M}" if self.started else "") if b))
            if row:
                lobby = row[22] or lobby
                bits = [f"squad kills {row[1]}" if row[1] != "" else "", f"landed {row[20]}" if row[20] else "",
                        "hot drop" if row[21] == "Yes" else "", f"died at {row[12]}" if row[12] else "",
                        f"lobby: {row[22]}" if row[22] else "",
                        f"lobby skill {lobby_position(row[23])}/100" if row[23] != "" else ""]
            if game and is_ranked(game) and game["rp"] is not None:
                bits.append(f"{game['rp']:+,.0f} RP" + ("" if game.get("rp_real") is not None else " (estimate)"))
            elif row and isinstance(row[34], (int, float)):     # the read's real RP, before the workbook has it
                bits.append(f"{row[34]:+,.0f} RP")
            bits.append("in the workbook" if game else "read off the stats screen - landing, death zone and the workbook "
                        "once the match is read" if early else "landing, death zone and the workbook once the map's worked out"
                        if stats.get("quick") else "not in the workbook yet")
            bits.append(self.movement_line(won=bool(row) and str(row[0]) == "1"))
            players = [dict({k: (None if v == "" else v) for k, v in p.items()},
                            **{"Survival Time": seconds(p["Survival Time"])}) for p in squad]
        else:
            self.heading.configure(text=" · ".join(b for b in (
                mode, map_name, f"started {self.started:%H:%M}" if self.started else "") if b))
            if self.reading:
                bits.append("reading legend select...")
            elif not picks:
                bits.append("legend select not seen yet" if not self.picks_read else "legend select couldn't be read")
            else:
                bits.append("stats come in once the match has been read")
            if lobby:
                bits.insert(0, f"lobby: {lobby}")
            players = [{"Player": p.get("name"), "Legend": p.get("legend"), "Rank": p.get("rank")}
                       for p in picks.get("squad", [])]
        self.subheading.configure(text="  ·  ".join(b for b in bits if b))
        # Squad rows without a Rank (the stats screen read early): the legend select badges by name
        ranks = {p.get("name"): p.get("rank") for p in picks.get("squad", []) if p.get("rank")}
        for p in players:
            if not p.get("Rank"):
                p["Rank"] = next((r for n, r in ranks.items()
                                  if Extract_Stats.name_similarity(n, p.get("Player")) >= 0.6), None)
        self.squad.show([dict(p, Player=(p.get("Player") or "?") + ("  (you)" if Extract_Stats.is_me(p.get("Player")) else ""),
                              in_lobby=lobby_standing(p.get("Rank"), lobby))
                         for p in players])


# ---------------------------
# CAPTURE TAB
# ---------------------------

def unit_state(unit):
    r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def match_folders():
    if not CAPTURES.is_dir():
        return []
    return sorted((p.name for p in CAPTURES.iterdir() if p.is_dir() and MATCH_FOLDER.match(p.name)),
                  reverse=True)


class CaptureTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.output = queue.Queue()       # lines from background commands, shown in the log
        self.job = None
        self.log_pos = None
        self.map_mtime = None
        self.map_photo = None

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=18, height=18, highlightthickness=0, background=BG)
        self.dot.pack(side="left")
        self.dot_item = self.dot.create_oval(3, 3, 15, 15, fill="grey", outline="")
        self.status = ttk.Label(top, text="Capture: checking...", font=("TkDefaultFont", 11, "bold"))
        self.status.pack(side="left", padx=(6, 20))
        ttk.Button(top, text="Restart now", command=self.restart_now).pack(side="left", padx=2)
        ttk.Button(top, text="Restart after match", command=self.restart_after_match).pack(side="left", padx=2)
        ttk.Button(top, text="Stop", command=self.stop).pack(side="left", padx=2)

        middle = ttk.Frame(self)
        middle.pack(fill="x", pady=10)

        book = ttk.LabelFrame(middle, text="Workbook", padding=8)
        book.pack(side="left", fill="both", expand=True)
        buttons = [("Add new matches", []), ("Refresh rows", ["--refresh"]), ("Fill in blanks", ["--fill-in"]),
                   ("Rebuild Squad Stats", ["--rebuild-squad"]), ("Unhide tabs", ["--unhide"])]
        for i, (label, args) in enumerate(buttons):
            ttk.Button(book, text=label, command=lambda a=args, l=label: self.sheet_write(a, l)).grid(
                row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
        ttk.Button(book, text="Open workbook", command=lambda: open_path(Seasons.workbook())).grid(
            row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Separator(book).grid(row=2, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(book, text="Match:").grid(row=3, column=0, sticky="w")
        self.match = ttk.Combobox(book, values=match_folders(), state="readonly", width=22,
                                  postcommand=lambda: self.match.configure(values=match_folders()))
        self.match.grid(row=3, column=1, sticky="ew", padx=2)
        match_buttons = ttk.Frame(book)
        match_buttons.grid(row=3, column=2, sticky="ew")
        ttk.Button(match_buttons, text="Open", width=6, command=self.open_match).pack(side="left", padx=2)
        ttk.Button(match_buttons, text="Remove", width=8, command=self.remove_match).pack(side="left", padx=2)
        for c in range(3):
            book.columnconfigure(c, weight=1)

        preview = ttk.LabelFrame(middle, text="Latest match map", padding=4)
        preview.pack(side="left", padx=(10, 0))
        self.map_label = ttk.Label(preview, text="(no picture yet)", anchor="center", cursor="hand2")
        self.map_label.pack()
        self.map_label.bind("<Button-1>", lambda e: open_path(LATEST_MAP))

        logs = ttk.LabelFrame(self, text="capture.log", padding=4)
        logs.pack(fill="both", expand=True)
        self.log = tk.Text(logs, height=16, wrap="none", font=("TkFixedFont", 9), state="disabled",
                           background=SURFACE, foreground="#d4d4d4", insertbackground=TEXT_COLOUR,
                           relief="flat", highlightthickness=0)
        scroll = ttk.Scrollbar(logs, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("cmd", foreground="#7fb8ff")
        self.log.tag_configure("problem", foreground="#ff8a80")
        # Always at the newest line - also when the tab comes back into view (scrolling while it's hidden doesn't take)
        self.log.bind("<Map>", lambda e: self.log.see("end"))
        self.log.bind("<Configure>", lambda e: self.log.see("end"))

        self.poll_status()
        self.poll_log()
        self.poll_map()

    # --- service ---

    def poll_status(self):
        state = unit_state(SERVICE)
        colour = {"active": "#3cb44b", "activating": "#ffb000", "failed": "#e6194b"}.get(state, "grey")
        text = {"active": "running", "inactive": "stopped"}.get(state, state)
        if unit_state(RESTART_LATER) == "active":
            text += "  (restart queued: once this match is in the workbook, at the next lobby)"
        self.dot.itemconfigure(self.dot_item, fill=colour)
        self.status.configure(text=f"Capture: {text}")
        self.after(3000, self.poll_status)

    def restart_now(self):
        if unit_state(SERVICE) == "active" and not messagebox.askyesno(
                "Restart now?", "Restarting now cuts a match that's in progress.\n"
                "Mid-match, use \"Restart after match\" instead.\n\nRestart now?"):
            return
        self.run(["./restart_capture.sh"], "Restart capture")

    def restart_after_match(self):
        self.run(["systemd-run", "--user", f"--unit={RESTART_LATER}", f"--working-directory={HERE}",
                  str(HERE / "restart_capture.sh"), "--after-match"], "Restart after match")

    def stop(self):
        if messagebox.askyesno("Stop capture?", "Stop recording stats until you restart it?"):
            self.run(["systemctl", "--user", "stop", SERVICE], "Stop capture")

    # --- workbook ---

    def sheet_write(self, args, label):
        if args == ["--rebuild-squad"] and not messagebox.askyesno(
                "Rebuild Squad Stats?", "Start the Squad Stats tab over from every captured match?"):
            return
        self.run([str(PYTHON), "Sheet_Write.py"] + args, label)

    def remove_match(self):
        name = self.match.get()
        if not name:
            messagebox.showinfo("Remove match", "Pick a match first.")
            return
        if messagebox.askyesno("Remove match?", f"Take {name} back out of the workbook?"):
            self.run([str(PYTHON), "Sheet_Write.py", "--remove", name], f"Remove {name}")

    def open_match(self):
        if self.match.get():
            folder = CAPTURES / self.match.get()
            picture = folder / "match_map.jpg"
            open_path(picture if picture.exists() else folder)

    # --- running commands ---

    def run(self, cmd, label):
        if self.job and self.job.is_alive():
            messagebox.showinfo("Busy", "Another command is still running, wait for it to finish.")
            return

        def work():
            self.output.put(("cmd", f"▶ {label}"))
            try:
                p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    self.output.put(("cmd", "  " + line.rstrip()))
                code = p.wait()
                self.output.put(("cmd" if code == 0 else "problem",
                                 f"■ {label}: " + ("done" if code == 0 else f"failed (exit {code})")))
            except OSError as e:
                self.output.put(("problem", f"■ {label}: {e}"))

        self.job = threading.Thread(target=work, daemon=True)
        self.job.start()

    # --- log + picture ---

    def poll_log(self):
        lines = []
        try:
            size = LOG.stat().st_size
            if self.log_pos is None or size < self.log_pos:      # first look, or the log was started over
                self.log_pos = max(0, size - 64 * 1024)
            if size > self.log_pos:
                with open(LOG, "rb") as f:
                    f.seek(self.log_pos)
                    data = f.read()
                self.log_pos += len(data)
                lines = [(None, l) for l in data.decode("utf-8", "replace").splitlines()]
        except OSError:
            pass
        while not self.output.empty():
            lines.append(self.output.get())
        if lines:
            self.log.configure(state="normal")
            for tag, line in lines:
                if tag is None and re.search(r"Traceback|Error|error|failed", line):
                    tag = "problem"
                self.log.insert("end", line + "\n", tag or ())
            extra = int(self.log.index("end-1c").split(".")[0]) - LOG_LINES
            if extra > 0:
                self.log.delete("1.0", f"{extra + 1}.0")
            self.log.configure(state="disabled")
            self.log.see("end")
        self.after(1000, self.poll_log)

    def poll_map(self):
        try:
            mtime = LATEST_MAP.stat().st_mtime
            if mtime != self.map_mtime:
                image = Image.open(LATEST_MAP)
                image.thumbnail((260, 260))
                self.map_photo = ImageTk.PhotoImage(image)
                self.map_label.configure(image=self.map_photo, text="")
                self.map_mtime = mtime
        except (OSError, SyntaxError):       # missing, or caught half-written
            pass
        self.after(5000, self.poll_map)


# ---------------------------
# MAIN WINDOW
# ---------------------------

class App(ttk.Frame):
    """The filters bar, the tabs, and keeping the stats in step with the workbook."""

    def __init__(self, root):
        super().__init__(root)
        self.pack(fill="both", expand=True)
        bar = ttk.Frame(self, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        self.filters = {}
        for label, width in (("Season", 7), ("Mode", 10), ("Map", 14), ("Legend", 13), ("Period", 14)):
            ttk.Label(bar, text=label + ":").pack(side="left")
            box = ttk.Combobox(bar, state="readonly", width=width, values=PERIODS if label == "Period" else [ALL])
            box.set(PERIODS[0] if label == "Period" else ALL)
            box.pack(side="left", padx=(4, 12))
            box.bind("<<ComboboxSelected>>", lambda e: self.show())
            self.filters[label] = box
        ttk.Button(bar, text="Clear filters", command=self.clear_filters).pack(side="left")
        ttk.Button(bar, text="Reload", command=self.reload).pack(side="right")
        self.status = ttk.Label(self, text="Reading the workbooks...", foreground=MUTED, padding=(10, 2, 10, 4))
        self.status.pack(side="bottom", fill="x")

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.latest, self.overview, self.squad = LatestTab(tabs), OverviewTab(tabs), SquadTab(tabs)
        self.charts, self.matches, self.maps = ChartsTab(tabs), MatchesTab(tabs), MapsTab(tabs)
        tabs.add(self.latest, text="  Latest Match  ")
        tabs.add(self.overview, text="  Overview  ")
        tabs.add(self.squad, text="  Squad  ")
        tabs.add(self.charts, text="  Charts  ")
        tabs.add(self.matches, text="  Matches  ")
        tabs.add(self.maps, text="  Maps  ")
        tabs.add(CaptureTab(tabs), text="  Capture  ")

        self.games, self.loaded_mtime, self.loading = [], None, False
        self.results = queue.Queue()
        self.reload()
        self.poll()

    def reload(self):
        if self.loading:
            return
        self.loading = True
        self.status.configure(text="Reading the workbooks...")

        def work():
            try:
                mtime = sources_saved()
                self.results.put((mtime, load_stats()))
            except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError, ValueError) as e:   # e.g. mid-save
                self.results.put((None, e))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        """Every couple of seconds: take a finished read, and re-read when the workbook changed."""
        while not self.results.empty():
            mtime, result = self.results.get()
            self.loading = False
            if mtime is None:
                self.status.configure(text=f"Couldn't read the workbooks ({result.__class__.__name__}), retrying...")
            else:
                self.loaded_mtime, self.games = mtime, result
                self.fill_filters()
                self.show()
        try:
            if not self.loading and sources_saved() != self.loaded_mtime:
                self.reload()
        except OSError:
            pass
        self.maps.poll()
        self.after(3000, self.poll)

    def fill_filters(self):
        self.filters["Season"].configure(values=[ALL] + [f"S{s}" for s in sorted({g["season"] for g in self.games},
                                                                                  reverse=True)])
        for label, field in (("Mode", "mode"), ("Map", "map"), ("Legend", "legend")):
            counts = Counter(g[field] for g in self.games if g[field])
            self.filters[label].configure(values=[ALL] + [name for name, _ in counts.most_common()])

    def clear_filters(self):
        for label, box in self.filters.items():
            box.set(PERIODS[0] if label == "Period" else ALL)
        self.show()

    def filtered(self, ignore_map=False):
        games = self.games
        if self.filters["Season"].get() != ALL:
            games = [g for g in games if f"S{g['season']}" == self.filters["Season"].get()]
        for label, field in (("Mode", "mode"), ("Map", "map"), ("Legend", "legend")):
            wanted = self.filters[label].get()
            if wanted != ALL and not (ignore_map and field == "map"):
                games = [g for g in games if g[field] == wanted]
        period, now = self.filters["Period"].get(), datetime.now()
        if period == "Today":
            games = [g for g in games if g["time"].date() == now.date()]
        elif period.startswith("Last") and period.endswith("days"):
            since = now - timedelta(days=int(period.split()[1]))
            games = [g for g in games if g["time"] >= since]
        elif period.startswith("Last"):
            games = games[:int(period.split()[1])]                   # newest first
        return games

    def show(self):
        games = self.filtered()
        self.latest.show(self.games)
        self.overview.show(games)
        self.squad.show(games)
        self.charts.show(games)
        self.matches.show(games)
        self.maps.show(self.filtered(ignore_map=True))
        shown = f"{len(games)} of {len(self.games)} games" if len(games) != len(self.games) else f"{len(games)} games"
        when = datetime.fromtimestamp(max(self.loaded_mtime)).strftime("%d %b %H:%M") if self.loaded_mtime else "?"
        self.status.configure(text=f"{shown} · workbook saved {when}")


def dark_theme(root):
    """Dark colours for every widget: ttk through the "clam" theme, plain Tk ones (the combobox drop-downs,
    message boxes) through the option database."""
    root.configure(background=BG)
    for pattern, value in (("*Background", BG), ("*Foreground", TEXT_COLOUR), ("*selectBackground", SELECTED),
                           ("*selectForeground", TEXT_COLOUR), ("*activeBackground", HOVER),
                           ("*activeForeground", TEXT_COLOUR), ("*highlightBackground", BG),
                           ("*TCombobox*Listbox.background", SURFACE), ("*TCombobox*Listbox.foreground", TEXT_COLOUR),
                           ("*TCombobox*Listbox.selectBackground", SELECTED)):
        root.option_add(pattern, value)
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT_COLOUR, fieldbackground=SURFACE, bordercolor=BORDER,
                    lightcolor=RAISED, darkcolor=BG, troughcolor=SURFACE, selectbackground=SELECTED,
                    selectforeground=TEXT_COLOUR, insertcolor=TEXT_COLOUR, arrowcolor=MUTED, focuscolor=BORDER)
    style.map(".", foreground=[("disabled", FAINT)])
    style.configure("TButton", background=RAISED, padding=(10, 4))
    style.map("TButton", background=[("disabled", BG), ("pressed", SELECTED), ("active", HOVER)])
    style.configure("Toolbutton", background=RAISED, padding=(10, 4))
    style.map("Toolbutton", background=[("selected", SELECTED), ("active", HOVER)])
    style.configure("TCombobox", background=RAISED, arrowcolor=TEXT_COLOUR)
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)], background=[("active", HOVER)],
              selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", TEXT_COLOUR)])
    style.configure("TSpinbox", background=RAISED, arrowcolor=TEXT_COLOUR)
    style.configure("TCheckbutton", indicatorbackground=SURFACE, indicatorforeground=TEXT_COLOUR)
    style.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(10, 4, 10, 0))
    style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED, bordercolor=BORDER, padding=(8, 4))
    style.map("TNotebook.Tab", background=[("selected", RAISED), ("active", HOVER)],
              foreground=[("selected", TEXT_COLOUR)], lightcolor=[("selected", ACCENT)])
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT_COLOUR,
                    bordercolor=BORDER, rowheight=22)
    style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", TEXT_COLOUR)])
    style.configure("Treeview.Heading", background=RAISED, foreground=TEXT_COLOUR, bordercolor=BORDER,
                    font=("TkDefaultFont", 9, "bold"))
    style.map("Treeview.Heading", background=[("active", HOVER)])
    style.configure("TScrollbar", background=RAISED, bordercolor=BG, troughcolor=BG, arrowcolor=MUTED)
    style.map("TScrollbar", background=[("active", HOVER)])
    style.configure("Sash", background=BORDER, bordercolor=BG, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("TSeparator", background=BORDER)
    style.configure("Tile.TFrame", background=RAISED, relief="flat", borderwidth=0)
    style.configure("TileName.TLabel", background=RAISED, foreground=MUTED, font=("TkDefaultFont", 9))
    style.configure("TileValue.TLabel", background=RAISED, foreground=TEXT_COLOUR, font=("TkDefaultFont", 17, "bold"))
    style.configure("TileText.TLabel", background=RAISED, foreground=TEXT_COLOUR, font=("TkDefaultFont", 12, "bold"))
    style.configure("TileNote.TLabel", background=RAISED, foreground=FAINT, font=("TkDefaultFont", 8))


def main():
    root = tk.Tk()
    root.title("Apex Stat Tracker")
    root.geometry("1180x820")
    root.minsize(980, 680)
    dark_theme(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
