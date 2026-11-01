"""
Read the stats out of the screenshots Stat_Capture.py saved.

For every match folder in Captures/ this writes stats.json (everything it could read,
including every squad member's stats), then rebuilds two spreadsheets:

    Captures/Sheet Rows.csv   - one row per match in the same column order as the
                                "Past Games" sheet (columns A..AH), ready to paste
    Captures/Squad Stats.csv  - one row per player per match (you and teammates)

Usage:
    python Extract_Stats.py                 # new/changed matches only
    python Extract_Stats.py --force         # re-read every match
    python Extract_Stats.py Captures/<match folder> [...]
"""
import argparse
import csv
import difflib
import json
import time
import re
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from Screen_Detection import load_config, scale_box, crop

# ---------------------------
# CONFIGURATION
# ---------------------------

CAPTURES_FOLDER = Path(__file__).parent / "Captures"
SHEET_FILE = CAPTURES_FOLDER / "Sheet Rows.csv"
SQUAD_FILE = CAPTURES_FOLDER / "Squad Stats.csv"
KEEP_SCREENSHOTS = False       # True keeps every screenshot of a match after it's been read
KEEP_FOR_MINUTES = 20          # screenshots stay this long after a match is read (to look at / test), then go
OVER_AFTER_MINUTES = 30        # newest match with nothing saved for this long is over (a game lasts up to ~20 min)
KEPT_FILES = {"stats.json", "match.json", "match_paths.svg", "trails.json", "match_map.jpg", "summary.jpg",
              "sheet.json", "fixes.json", ".stitched", ".cleaned", "rank_distribution.jpg"}
STATS_VERSION = 2              # bump to make old stats.json files get re-read
OCR_THREADS = 2

PLAYER_NAME = "Spookyrabit"    # your gamertag, without the [LIVE]/[BOT] club tag
FRIENDS = {}                   # gamertag -> what goes in the Friends column, e.g. {"SomeTag": "Lobster"}

LEGENDS = ["Alter", "Ash", "Axle", "Ballistic", "Bangalore", "Bloodhound", "Catalyst", "Caustic",
           "Conduit", "Crypto", "Fuse", "Gibraltar", "Horizon", "Lifeline", "Loba", "Mad Maggie",
           "Mirage", "New Castle", "Octane", "Pathfinder", "Rampart", "Revenant", "Seer", "Sparrow",
           "Valkyrie", "Vantage", "Wattson", "Wraith"]
MAPS = ["Worlds Edge", "Storm Point", "Broken Moon", "Olympus", "E-District", "Kings Canyon"]
MODES = ["Ranked", "Trios", "Duos", "Wildcard", "Bot Royale"]
# Respawn modes: you come back straight away while the squad lives, so trails jump across the map, and the rings
# start at round 1: kept out of each map's heatmaps, rings picture, endgame guess and learned ring sizes
RESPAWN_MODES = {"Wildcard"}
SKILL_GRAPH_MODES = {"Trios", "Duos"}   # lobbies with the "Players Skill Distribution" graph
# Mixtape modes and Bot Royale (Trios against bots, rings from round 1): never in the workbook / CSVs, and they
# teach the maps nothing
UNTRACKED_MODES = ["Control", "Gun Run", "Team Deathmatch", "Mixtape", "Bot Royal"]   # (matches "Bot Royale" too)

# Skill graph -> "Percentile" column. The three numbers, rounded to 5s.
PERCENTILE_PARTS = ("start", "team", "end")
PERCENTILE_SCALE, PERCENTILE_OFFSET = 1.0, 0.0   # straight from the graph (0.811, 17.0 = how you'd read it by eye)

SHEET_HEADER = ["Placement", "Squad Kills", "Kills", "Assists", "Knocks", "Time", "Revives", "Respawns",
                "Damage", "Legend", "Time", "Most Damage?", "Death Zone", "", "Game Mode", "Map",
                "Freinds", "", "", "Entery", "POI", "Contested", "Rank Distribution", "", "Dropship Heading", "EST XP",
                "Percentile", "Skill", "Squad", "Kills RP", "Placement RP", "Participation RP",
                "Loss Forgivness", "Ranked RP"]
# One row per player per match. Match-level columns repeat on each player's row so any row stands alone.
SQUAD_HEADER = ["Match", "Time", "Game Mode", "Map", "Placement", "Squad Kills", "Entry", "Percentile",
                "Dropship Heading", "Flight Path", "POI", "Death Zone", "Contested", "Survey Scans", "Stats Screen",
                "Slot", "Player", "Is Me", "Legend", "Teammates", "Squad", "Kills", "Assists", "Knocks",
                "Damage", "Damage %", "Survival Time", "Revives", "Respawns"]

# OCR look-alikes for digits (the recognizer is Chinese+English, so "0" can come back as 口)
DIGIT_FIXES = str.maketrans({"口": "0", "□": "0", "O": "0", "o": "0", "D": "0", "。": "0", "○": "0",
                             "Q": "0", "l": "1", "I": "1", "L": "1", "|": "1", "i": "1", "S": "5",
                             "s": "5", "B": "8", "Z": "2", "z": "2", "g": "9", "；": ":", ";": ":",
                             "：": ":", "／": "/", " ": ""})

# Summary card: label -> offset (px) below the "Kills / Assists / Knocks" label, used if OCR misses a label
CARD_STATS = {"kak": ("Kills", 0), "damage": ("Damage", 71), "time": ("Survival", 140),
              "revives": ("Revive", 209), "respawns": ("Respawn", 281)}
# Rows some modes show in their place (Wildcard: Last Stands / Self Revives): kept under their own name, and the
# Revives / Respawns they stand in for are left blank rather than filled with the wrong numbers
CARD_STATS_ALT = {"revives": ("Last Stand", "last_stands"), "respawns": ("Self Revive", "self_revives")}

# Skill graph track in 1920x1080 pixels
GRAPH_TRACK = (660, 1260)
GRAPH_ROWS = (944, 1008)

# Ranked lobby's "Squad Rank Distribution" bar in 1920x1080 pixels: one segment per rank, as wide as its share of the
# squads, the count in it, the rank icon above. -> "Rank Distribution" column (W), e.g. "Gold 4, Platinum 16"
RANK_BAR = (644, 1277)
RANK_BAR_ROWS = (996, 1031)
RANK_BAR_PLAIN = (997, 1004)     # rows above the count: one flat colour (the lower half is dotted)
RANK_LOBBY_SQUADS = 20           # the whole bar, for a segment whose count can't be read
RANK_BAR_CROP = (620, 925, 1300, 1040)   # kept as rank_distribution.jpg (bar + icons), to check RANK_COLOURS by
# Each rank's bar colour (RGB), lowest first, official names. Platinum and Diamond are measured on the bar
# (2026-10-31 18:49 and 20:19); the others are the median bright colour of each rank's emblem on the Ranked Leagues
# page - the bar is more saturated than the emblems (Diamond's emblem 84, 134, 247), so check them on a kept
# rank_distribution.jpg and move them here once seen.
RANK_COLOURS = {"Rookie": (209, 181, 153), "Bronze": (183, 121, 90), "Silver": (181, 189, 194), "Gold": (225, 177, 86),
                "Platinum": (78, 217, 246), "Diamond": (12, 140, 248), "Master": (183, 0, 246),
                "Apex Predator": (245, 3, 18)}
RANKS_MEASURED = {"Platinum", "Diamond"}
# The SQUAD RANK DISTRIBUTION title anchor scores 0.96-0.99 on Ranked's lobby, but 0.60-0.64 on the Trios / Duos
# lobby's PLAYERS SKILL DISTRIBUTION in the same spot - enough for the capture's "a lobby" (0.6), not for "Ranked"
RANK_TITLE_MATCH = 0.85
RANKS = list(RANK_COLOURS)

# ---------------------------
# OCR HELPERS
# ---------------------------

_ocr = None

def ocr_engine():
    global _ocr
    if _ocr is None:
        import tempfile
        import rapidocr_onnxruntime
        from rapidocr_onnxruntime import RapidOCR
        # A couple of threads is plenty and keeps the game/OBS smooth if this runs mid-session.
        # RapidOCR only honours thread counts from its config file, so hand it an edited copy.
        default = Path(rapidocr_onnxruntime.__file__).parent / "config.yaml"
        text = re.sub(r"(&(?:intra|inter)_nums) -1", rf"\1 {OCR_THREADS}", default.read_text())
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
        _ocr = RapidOCR(config_path=f.name)
        Path(f.name).unlink()
    return _ocr

def read_text(image):
    """Full OCR: list of (x, y, text) with x, y at the box's top-left."""
    result, _ = ocr_engine()(image)
    return [(int(box[0][0]), int(box[0][1]), text) for box, text, _ in (result or [])]

def read_line(image):
    """Recognition only, for a crop that holds one line of text."""
    result, _ = ocr_engine()(image, use_det=False, use_cls=False)
    return result[0][0] if result else ""

def clean_value_crop(region):
    """Keep only the bright text at the left of a value region, as black-on-white with padding."""
    v = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)[:, :, 2]
    _, mask = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n <= 1:
        return None
    tallest = stats[1:, cv2.CC_STAT_HEIGHT].max()
    keep = np.zeros_like(mask)
    for i in range(1, n):
        _, _, w, h, area = stats[i]
        if h >= 0.5 * tallest or (h >= 3 and w >= 2 and area >= 6):  # strokes, plus colon dots
            keep[labels == i] = 255
    cols = np.nonzero(keep.max(axis=0))[0]
    # Stop at the first big gap: anything past it is banner art or a card border
    end = cols[0]
    for c in cols:
        if c - end > 18:
            break
        end = c
    keep = keep[:, cols[0]:end + 1]
    rows = np.nonzero(keep.max(axis=1))[0]
    keep = keep[rows[0]:rows[-1] + 1]
    keep = cv2.copyMakeBorder(255 - keep, 12, 12, 24, 24, cv2.BORDER_CONSTANT, value=255)
    keep = cv2.resize(keep, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(keep, cv2.COLOR_GRAY2BGR)

def read_value(image, box):
    """Read a number / time / K-A-K value from a box, with digit look-alikes fixed."""
    region = crop(image, box)
    if region.size == 0:
        return ""
    cleaned = clean_value_crop(region)
    return read_line(cleaned).translate(DIGIT_FIXES) if cleaned is not None else ""

def squash(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())

def best_match(text, options, cutoff=0.6):
    """Fuzzy-match OCR text to a known name (ignores spaces, punctuation and case)."""
    squashed = {squash(o): o for o in options}
    hit = difflib.get_close_matches(squash(text), squashed, n=1, cutoff=cutoff)
    return squashed[hit[0]] if hit else None

def to_int(text):
    # "1,344" -> 1344 (thousands separators; OCR sometimes reads the comma as a dot)
    m = re.search(r"\d+", re.sub(r"(?<=\d)[,.](?=\d{3})", "", text or ""))
    return int(m.group()) if m else None

def is_me(name):
    """Your name, even cut off ("Spookyrabi" on the end card) or with a club tag in front."""
    n, mine = squash(name or ""), squash(PLAYER_NAME)
    if not n:
        return False
    shared = difflib.SequenceMatcher(None, n, mine).find_longest_match(0, len(n), 0, len(mine)).size
    return shared >= min(len(mine), 8)

def name_similarity(a, b):
    """Names from different screens: the end card cuts long names off, OCR may slip a letter."""
    a, b = squash(a or ""), squash(b or "")
    if not a or not b:
        return 0.0
    if a.startswith(b) or b.startswith(a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def player_zone(name, location):
    """Where a teammate was last seen (the end of their trail) as the nearest POI - their death zone."""
    best = max((q for q in location.get("players_out", []) if not q.get("is_me")),
               key=lambda q: name_similarity(name, q.get("name")), default=None)
    if best and name_similarity(name, best.get("name")) >= 0.6:
        return best.get("zone", "")
    return ""

def squad_names(stats):
    """Everyone's name in your squad, from the end cards and legend select."""
    names = [p.get("name") for p in (stats.get("summary") or {}).get("players", [])]
    names += [p.get("name") for p in stats.get("legend_select", {}).get("squad", [])]
    return sorted({n for n in names if n and len(squash(n)) >= 4})

def panel_legend(name, stats):
    """The legend the squad panel's portrait showed for this player (stats["panel_legends"]), or ""."""
    panel = stats.get("panel_legends", {})
    best = max(panel, key=lambda n: name_similarity(name, n), default=None)
    return panel[best] if best and name_similarity(name, best) >= 0.7 else ""

def load_fixes(stats):
    """Captures/<match>/fixes.json as it is now (it can be edited long after the match was read), else the
    copy read with the match."""
    path = CAPTURES_FOLDER / stats["match"] / "fixes.json"
    return json.loads(path.read_text()) if path.exists() else stats.get("fixes", {})

def assign_legends(players, picks):
    """Pair end-card players with legend select picks, most similar names first (only 2-3 of each)."""
    pairs = sorted(((name_similarity(p.get("name"), k.get("name")), i, j)
                    for i, p in enumerate(players) for j, k in enumerate(picks)), reverse=True)
    legends, used = {}, set()
    for score, i, j in pairs:
        if score >= 0.4 and i not in legends and j not in used:
            legends[i] = picks[j].get("legend", "")
            used.add(j)
    return [legends.get(i, "") for i in range(len(players))]

# ---------------------------
# SCREEN PARSERS
# ---------------------------

def parse_mode_map(lines):
    """Top-left "TRIOS / WORLD'S EDGE" on the waiting and legend select screens. Ranked's lobby ("Squad Rank
    Distribution") has them bigger and further apart: the map ~y 204 - only a known map name counts that far
    down (legend select's player names start at ~y 131)."""
    corner = sorted((y, t) for x, y, t in lines if x < 400 and y < 240)
    out = {}
    for y, text in corner:
        # Ranked - Dropzone: "RANKED - DROPZONE" (the lobby cuts it to "DROPZO"), squads spawn on the ground; legend
        # select says where: "WORLD'S EDGE - DROPPING AT: BIG MAUDE"
        if y < 110 and "DROPZO" in squash(text):
            out["dropzone"] = True
            text = re.split(r"-\s*DROPZ", text, flags=re.I)[0]
        m = re.search(r"-?\s*DROPPING\s*AT\s*:?\s*(.*)", text, re.I)
        if m:
            text = text[:m.start()]
            if m.group(1).strip():
                out["drop_poi"] = m.group(1).strip()
        if "mode" not in out and y < 110 and best_match(text, MODES):
            out["mode"] = best_match(text, MODES)
        elif "map" not in out and best_match(text, MAPS):
            out["map"] = best_match(text, MAPS)
    corner = [(y, t) for y, t in corner if y < 110]
    if "mode" not in out and corner:
        out["mode"] = corner[0][1].title()   # an unlisted mode (Mixtape etc.): keep what's shown
    return out

def parse_legend_select(image):
    lines = read_text(image)
    out = parse_mode_map(lines)
    # Bottom bar: legend names on one row, player names just below, one slot per third of the screen
    slots = {}
    for x, y, text in lines:
        if y < 990:
            continue
        slot = min(2, x * 3 // image.shape[1])
        entry = slots.setdefault(slot, {"slot": slot + 1})
        legend = best_match(text, LEGENDS, cutoff=0.75)
        if y < 1030 and legend:
            entry["legend"] = legend
        elif y >= 1030:
            entry["name"] = re.sub(r"^\[[^\]]*\]\s*", "", text)   # drop the [LIVE]/[BOT] club tag
    # A slot with a player but no legend yet ("NOW SELECTING") still counts as a squad member
    out["squad"] = [slots[s] for s in sorted(slots) if "legend" in slots[s] or slots[s].get("name")]
    if not out["squad"]:
        # Wildcard has no bottom bar: the names are top left in pick order (~y 130 / 210 / 290), and only your own
        # legend is spelled out (big, top right; the skin name below it can hold the legend's name too). Teammates'
        # legends then come from the squad panel portraits (Legend_Icons)
        s = image.shape[1] / CONFIG["base_size"][0]
        mine = next((best_match(t, LEGENDS, cutoff=0.75) for x, y, t in lines if x > 1450 * s and y < 170 * s
                     and best_match(t, LEGENDS, cutoff=0.75)), None)
        names = sorted((y, t) for x, y, t in lines if 120 * s < x < 450 * s and 110 * s < y < 330 * s)
        for slot, (_, text) in enumerate(names[:3], 1):
            name = re.sub(r"^\[[^\]]*\]\s*", "", text)
            out["squad"].append(dict(slot=slot, name=name, **({"legend": mine} if mine and is_me(name) else {})))
    return out

def read_compass(crop):
    """The heading number on a compass crop (Captures/<match>/compass/), or None."""
    h, w = crop.shape[:2]                         # the crop is 1920x1080 px (900, 70) - (1020, 130)
    inner = crop[int(h * 14 / 60):int(h * 54 / 60), int(w * 25 / 120):int(w * 95 / 120)]   # between the chevrons
    big = cv2.resize(inner, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    for _, _, text in sorted(read_text(big)):
        m = re.fullmatch(r"\D?(\d{1,3})\W*", text.strip())      # (a compass letter can sneak in front)
        if m and int(m.group(1)) < 360:
            return int(m.group(1))
    return None

def steady_heading(folder, events, hold=3):
    """The dropship heading: the first compass reading that stays the same for `hold` seconds in a row
    (on the ship, looking forward). None if it never settles."""
    readings = [read_compass(cv2.imread(str(folder / e["file"]))) for e in events if e["event"] == "compass"]
    run, last = 0, None
    for r in readings:
        run = run + 1 if r is not None and last is not None and min(abs(r - last), 360 - abs(r - last)) <= 2 else 1
        last = r
        if r is not None and run >= hold:
            return r
    return None

def parse_dropship(image):
    out = {}
    # Just the number between the compass chevrons, enlarged (the chevrons read as digits otherwise)
    inner = crop(image, scale_box([925, 84, 995, 124], CONFIG, image.shape[1]))
    big = cv2.resize(inner, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    for _, _, text in sorted(read_text(big)):
        m = re.fullmatch(r"(\d{1,3})\W*", text.strip())
        if m and int(m.group(1)) < 360:
            out["heading"] = int(m.group(1))
            break
    # Under the rank badge; Ranked - Dropzone (squads spawn on the ground, no flight) has it ~65 px lower
    for box in ([1780, 95, 1915, 135], [1780, 160, 1915, 200]):
        entry = read_line(crop(image, scale_box(box, CONFIG, image.shape[1])))
        m = re.search(r"-\s*(\d+)\s*RP", entry.upper().replace(" ", ""))
        if m:
            out["entry_rp"] = int(m.group(1))
            break
    return out

def parse_skill_graph(image):
    """Positions (0-100 along the track) of the shaded "this match" band and the red "your team" line."""
    s = image.shape[1] / CONFIG["base_size"][0]
    x1, x2 = (round(v * s) for v in GRAPH_TRACK)
    y1, y2 = (round(v * s) for v in GRAPH_ROWS)
    band = image[y1:y2, x1:x2].astype(int)
    b, g, r = band[:, :, 0], band[:, :, 1], band[:, :, 2]
    red_cols = np.nonzero(((r > 150) & (g < 90) & (b < 90)).sum(axis=0) > (y2 - y1) * 0.4)[0]
    if len(red_cols) == 0:
        return {}
    # The shaded band is a flat light grey; the rest of the track is darker and shows the scene behind
    gray = np.median(band.mean(axis=2), axis=0)
    shaded = np.nonzero((gray > 75) & (gray < 150))[0]
    if len(shaded) < 5:
        return {}
    width = x2 - x1
    pct = lambda col: round(100 * col / width, 1)
    return {"start": pct(shaded.min()), "end": pct(shaded.max() + 1), "team": pct(red_cols.mean())}

def parse_rank_distribution(image):
    """Ranked lobby's Squad Rank Distribution bar, left to right: [{"rank", "squads", "x1", "x2" (1080p px), "rgb",
    "off" (how far its colour is from the rank's), "ocr" (the number read in it), "read" (the OCR agrees)}], or []
    with no bar. The bar is cut where its colour changes, each part named by the nearest RANK_COLOURS. Counts come
    from the widths (the whole bar is RANK_LOBBY_SQUADS squads: 20 Platinum + Diamond lobbies came to 11.0 / 8.97
    squads wide, and OCR read that 9 as a 6), else from the OCR."""
    s = image.shape[1] / CONFIG["base_size"][0]
    x1, x2 = (round(v * s) for v in RANK_BAR)
    y1, y2 = (round(v * s) for v in RANK_BAR_PLAIN)
    cols = np.median(image[y1:y2, x1:x2], axis=0)[:, ::-1].astype(float)     # RGB per column
    if np.median(cols.max(axis=1)) < 120:
        return []                                  # no bright bar (still loading in, or not the ranked lobby)
    runs, start = [], 0
    for i in range(1, len(cols) + 1):
        if i == len(cols) or np.abs(cols[i] - np.median(cols[max(start, i - 10):i], axis=0)).max() > 45:
            runs.append([start, i])
            start = i
    runs = [r for r in runs if r[1] - r[0] >= 6 * s]          # the blend between two colours
    segments = []
    for a, b in runs:
        rgb = np.median(cols[a:b], axis=0)
        rank = min(RANKS, key=lambda k: np.linalg.norm(rgb - RANK_COLOURS[k]))
        if segments and segments[-1]["rank"] == rank:
            segments[-1]["x2"] = b
            continue
        segments.append({"rank": rank, "x1": a, "x2": b, "rgb": [int(v) for v in rgb],
                         "off": round(float(np.linalg.norm(rgb - RANK_COLOURS[rank])))})
    if not segments:
        return []
    segments[0]["x1"], segments[-1]["x2"] = 0, len(cols)       # the blends go to the parts either side
    for left, right in zip(segments, segments[1:]):
        left["x2"] = right["x1"] = (left["x2"] + right["x1"]) // 2
    # The counts: dark digits in the middle of each segment (one OCR of the whole bar; a crop the bar's width is
    # too wide for the line reader). Each number goes to the segment its box's middle is in.
    r1, r2 = (round(v * s) for v in RANK_BAR_ROWS)
    result, _ = ocr_engine()(image[max(0, r1 - round(8 * s)):r2 + round(8 * s), x1:x2])
    for seg in segments:
        seg["ocr"] = None
    for box, text, _ in result or []:
        middle = np.mean([p[0] for p in box])
        n = to_int(text.translate(DIGIT_FIXES))
        seg = next((g for g in segments if g["x1"] <= middle < g["x2"]), None)
        if seg and seg["ocr"] is None and n is not None and 1 <= n <= RANK_LOBBY_SQUADS:
            seg["ocr"] = n
    wide = [(g["x2"] - g["x1"]) * RANK_LOBBY_SQUADS / len(cols) for g in segments]      # squads by width
    if all(abs(w - round(w)) < 0.3 and round(w) >= 1 for w in wide) and sum(map(round, wide)) == RANK_LOBBY_SQUADS:
        for seg, w in zip(segments, wide):
            seg["squads"] = round(w)
    else:
        # Not a full lobby (or a shot mid-fade): counts that were read, the rest from their width at the px per
        # squad of those read
        read = [g for g in segments if g["ocr"]]
        per_squad = (sum(g["x2"] - g["x1"] for g in read) / sum(g["ocr"] for g in read) if read
                     else len(cols) / RANK_LOBBY_SQUADS)
        for seg in segments:
            seg["squads"] = seg["ocr"] or max(1, round((seg["x2"] - seg["x1"]) / per_squad))
    for seg in segments:
        seg["read"] = seg["ocr"] == seg["squads"]
        seg["x1"], seg["x2"] = RANK_BAR[0] + round(seg["x1"] / s), RANK_BAR[0] + round(seg["x2"] / s)
    return segments

def rank_shot_score(segments):
    """How believable one shot's bar is (lower is better), to pick between the lobby's shots: the last one can be
    the fade / wipe into legend select (2026-10-31 20:19: the dimmed bar read as Diamond 11). Colours away from
    their rank's, widths that aren't whole squads of a full lobby, counts the OCR disagrees with."""
    if not segments:
        return float("inf")
    width = sum(g["x2"] - g["x1"] for g in segments)
    score = sum(g["off"] * (g["x2"] - g["x1"]) / width for g in segments)
    score += 100 * (sum(g["squads"] for g in segments) != RANK_LOBBY_SQUADS)
    score += 20 * sum(not g["read"] for g in segments)
    return score

def best_rank_distribution(shots):
    """(name, segments) of the believable shot among [(name, image)], or (None, [])."""
    read = [(rank_shot_score(segs), i, name, segs)
            for i, (name, image) in enumerate(shots) for segs in [parse_rank_distribution(image)] if segs]
    if not read:
        return None, []
    _, _, name, segs = min(read)
    return name, segs

_rank_title = None

def shows_rank_distribution(image):
    """Is this Ranked's lobby? Its SQUAD RANK DISTRIBUTION title, the anchor the capture recognises it by (the RANKED
    corner animates in and can be half there), matched closely enough not to be PLAYERS SKILL DISTRIBUTION."""
    global _rank_title
    from Screen_Detection import ScreenClassifier, to_detect
    if _rank_title is None:
        _rank_title = ScreenClassifier()
    return _rank_title.anchor_score(to_detect(image, CONFIG), "waiting", "rank_distribution") >= RANK_TITLE_MATCH

def rank_distribution(stats):
    """The "Rank Distribution" cell: "Gold 4, Platinum 16" (lowest rank first)."""
    segments = sorted(stats.get("rank_distribution") or [], key=lambda g: RANKS.index(g["rank"]))
    return ", ".join(f"{g['rank']} {g['squads']}" for g in segments)

def find_label(lines, word, x, y_min, y_max):
    for lx, ly, text in lines:
        if abs(lx - x) < 40 and y_min <= ly <= y_max and squash(word) in squash(text):
            return ly
    return None

def parse_summary(image):
    if image.shape[1] != 1920:                    # card positions below are in 1080p pixels
        image = cv2.resize(image, (1920, round(1920 * image.shape[0] / image.shape[1])), interpolation=cv2.INTER_AREA)
    lines = read_text(image)
    # SQUAD ELIMINATED / SQUAD PLACED #n / CHAMPION over the cards - or just MATCH SUMMARY (2026-10-31 19:43, a #2:
    # no placement on it, that comes from the EXPERIENCE tab)
    if not any(squash(t).startswith("SQUAD") and "ELIMINATED" in squash(t) or "CHAMPION" in squash(t)
               or "MATCHSUMMARY" in squash(t) for _, _, t in lines) and not any("PLACED" in squash(t) for _, _, t in lines):
        return None
    out = {"players": []}
    for x, y, text in lines:
        m = re.fullmatch(r"#\s*(\d{1,2})", text.strip())
        if m and y < 260:
            out["placement"] = int(m.group(1))
        if squash(text) in ("TOTALKILLS", "WITHSQUAD") and "squad_kills" not in out:
            kills = to_int(read_value(image, (x + 110, y - 8, x + 200, y + 60)))
            if kills is not None:
                out["squad_kills"] = kills
    if "placement" not in out:
        # The big "#9" next to SQUAD / PLACED can read garbled on the whole screen: read it close up
        label = next(((x, y) for x, y, t in lines if squash(t) == "SQUAD" and y < 260), None)
        if label:
            x, y = label
            near = image[max(0, y - 30):y + 70, x + 40:x + 260]
            big = cv2.resize(near, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            for _, _, text in read_text(big):
                m = re.fullmatch(r"#?\s*(\d{1,2})", text.strip())
                if m and 1 <= int(m.group(1)) <= 30:
                    out["placement"] = int(m.group(1))
                    break
    # One card per "Kills / Assists / Knocks" label; works for 2 or 3 cards
    for kx, ky, text in sorted(lines):
        if not squash(text).startswith("KILLSASSIST"):
            continue
        card = {"x": kx}
        for x, y, name in lines:
            if abs(x - kx) < 60 and ky - 110 < y < ky - 50:
                card["name"] = re.sub(r"^\[[^\]]*\]\s*", "", name)
                # The whole-screen read can garble a name ("colddudde98" -> "6appnppioo"); a close-up of its
                # start reads it, but cuts long names short - extract_match keeps whichever legend select backs
                close = re.sub(r"^\[[^\]]*\]\s*", "", read_line(image[y - 7:y + 35, x - 6:x + 170])).strip()
                if close and close != card["name"]:
                    card["name_close"] = close
        for key, (word, offset) in CARD_STATS.items():
            ly = find_label(lines, word, kx, ky + offset - 25, ky + offset + 25)
            alt_word, alt_key = CARD_STATS_ALT.get(key, (None, None))
            alt = alt_word and not ly and find_label(lines, alt_word, kx, ky + offset - 25, ky + offset + 25)
            if alt:
                card[alt_key] = to_int(read_value(image, (kx - 4, alt + 18, kx + 110, alt + 52)))
                card[key] = None
                continue
            ly = ly or ky + offset
            card[key] = read_value(image, (kx - 4, ly + 18, kx + 110, ly + 52))
            # The close-up read can trip over the card art; the whole-screen read is the backup
            pattern = {"kak": r"\d+/\d+/\d+", "time": r"\d{1,2}:\d{2}"}.get(key, r"[\d,]+")
            below = [t.translate(DIGIT_FIXES) for x, y, t in lines if abs(x - kx) < 40 and ly + 12 <= y <= ly + 50]
            whole = next((t for t in below if re.fullmatch(pattern, t)), None)
            if not re.fullmatch(pattern, card[key] or ""):
                card[key] = whole or card[key]
            elif whole and len(whole) == len(card[key]) and all(
                    a == b or (a, b) == ("7", "1") for a, b in zip(card[key], whole)):
                card[key] = whole                 # this font's 1 reads as 7 close up: "7:19" was 1:19
        kak = re.fullmatch(r"(\d+)/(\d+)/(\d+)", card.pop("kak"))
        if kak:
            card["kills"], card["assists"], card["knocks"] = map(int, kak.groups())
        card["damage"] = to_int(card["damage"])
        card["revives"] = to_int(card["revives"])
        card["respawns"] = to_int(card["respawns"])
        t = re.search(r"(\d{1,2}):(\d{2})", card["time"])
        card["time"] = f"{int(t.group(1))}:{t.group(2)}" if t else None
        out["players"].append(card)
    if out["players"] and "squad_kills" not in out:
        out["squad_kills_from_cards"] = True
        out["squad_kills"] = sum(p.get("kills") or 0 for p in out["players"])
    return out

def parse_experience(image):
    """The EXPERIENCE tab (ranked): placement, cost of entry and the RP breakdown. None if it isn't that page."""
    lines = read_text(image)
    if not any("MATCHPLACEMENT" in squash(t) for _, _, t in lines):
        return None
    rows = {}
    for x, y, text in lines:
        rows.setdefault(round(y / 12), []).append((x, text))
    out = {}
    for _, parts in sorted(rows.items()):
        parts.sort()
        for i, (_, label) in enumerate(parts):
            key = squash(label)
            if key not in ("PLACEMENT", "COSTOFENTRY", "KILLS", "ASSISTS", "PARTICIPATION"):
                continue
            # The value is the first "#n" / "n RP" after the label (Kills has a count column first)
            for _, value in parts[i + 1:]:
                number = re.sub(r"^(-?)[OoD]", r"\g<1>0", value.replace(" ", "").upper())   # "ORP" is 0 RP
                if key == "PLACEMENT" and re.fullmatch(r"#\d{1,2}", number):
                    out["placement"] = int(number[1:])
                elif key != "PLACEMENT" and (m := re.fullmatch(r"(-?\d+)RP", number)):
                    out["entry_rp" if key == "COSTOFENTRY" else f"{key.lower()}_rp"] = abs(int(m.group(1))) if key == "COSTOFENTRY" else int(m.group(1))
                elif re.search(r"[A-Za-z]{4,}", value):
                    break                          # reached the next label: no value for this one
                else:
                    continue
                break
    return out or None

def summary_score(summary):
    """How complete a parsed summary is, to pick the best end-of-match screenshot."""
    if not summary:
        return -1
    fields = ("kills", "damage", "time", "revives", "respawns")
    return sum(p.get(f) is not None for p in summary["players"] for f in fields) + ("placement" in summary)

# ---------------------------
# MATCH -> ROW
# ---------------------------

def most_damage(mine, squad_damage):
    """-2 no damage, 2 at least half the squad's, 1 most, -1 least, 0 in between."""
    if mine is None or not squad_damage:
        return None
    if mine == 0:
        return -2
    if mine * 2 >= sum(squad_damage):
        return 2
    if mine >= max(squad_damage):
        return 1
    if mine <= min(squad_damage):
        return -1
    return 0

def est_xp(me, placement):
    """Same as the sheet: kills*50 + seconds*3 + damage/4 + revives*25 + respawns*200 + 300 for top 5."""
    m, s = map(int, me["time"].split(":"))
    xp = me["kills"] * 50 + (m * 60 + s) * 3 + me["damage"] / 4 + me["revives"] * 25 + me["respawns"] * 200
    if placement and placement <= 5:
        xp += 300
    return int(xp) if xp == int(xp) else xp

def percentile(graph):
    if not all(k in graph for k in PERCENTILE_PARTS):
        return ""
    # The graph position, 0-100 along the track
    read = lambda raw: min(100, max(0, PERCENTILE_SCALE * raw + PERCENTILE_OFFSET))
    parts = sorted(int(5 * round(read(graph[k]) / 5)) for k in PERCENTILE_PARTS)
    return "-".join(map(str, parts))

def skill(pct):
    if not pct:
        return ""
    a, b, c = map(int, pct.split("-"))
    return round((a * 2 + b + c * 4) / 7, 1)

def drop_poi(stats):
    """Where a Ranked - Dropzone squad spawned, as legend select names it, spelled like the map's POIs."""
    if not stats.get("drop_poi"):
        return ""
    import Map_Locate
    path = Map_Locate.map_folder(stats.get("map", "")) / "pois.json"
    names = [p["name"] for p in json.loads(path.read_text())["pois"]] if stats.get("map") and path.exists() else []
    return best_match(stats["drop_poi"], names) or stats["drop_poi"].title()

def tracked(stats):
    """Does this match go in the workbook (and the maps)? Not a Mixtape mode (Control / Gun Run / Team Deathmatch),
    and with no mode read, only if the battle royale HUD (SQUADS LEFT: what the in_game screen is recognised by)
    was seen - Mixtape has none. Matches read before this was kept count as tracked."""
    mode = squash(stats.get("mode") or "")
    if any(squash(m) in mode for m in UNTRACKED_MODES):
        return False
    return bool(mode) or stats.get("battle_royale_hud", True)

def build_rows(stats):
    """Turn a stats.json dict into the Past Games row (list) and the per-player Squad Stats rows (dicts)."""
    summary = stats.get("summary") or {}
    location = stats.get("location") or {}
    players = summary.get("players", [])
    me = next((p for p in players if is_me(p.get("name"))), None)
    fixes = load_fixes(stats)
    # Legends you typed in (Control_Panel: Matches > Set legends, by name as on the stats screen) beat legend
    # select, which beats the portraits on the squad panel (Legend_Icons: only read when legend select missed some)
    fixed = fixes.get("legends", {})
    # a player whose name wasn't read is keyed by position on the stats screen: "#1" = first card
    fixed_at = lambda i, name: fixed.get(name) or fixed.get(f"#{i + 1}")
    picks = [dict(k, legend=fixed.get(k.get("name")) or k.get("legend") or panel_legend(k.get("name"), stats))
             for k in stats.get("legend_select", {}).get("squad", [])]
    my_pick = next((p for p in picks if is_me(p.get("name"))), None)
    # Teammates in end-card order (left card, then right), like the sheet; a missing teammate is "Empty"
    player_legends = [fixed_at(i, p.get("name")) or l or panel_legend(p.get("name"), stats)
                      for i, (p, l) in enumerate(zip(players, assign_legends(players, picks)))]
    my_legend = next((l for p, l in zip(players, player_legends) if p is me and l), None) \
        or (my_pick.get("legend", "") if my_pick else "")
    # Teammates as in your sheet: everyone who picked a legend (even if they left before the end), in
    # end-card order; "Empty" only for a slot nobody was in
    mate_legends = [l for p, l in zip(players, player_legends) if p is not me and l]
    on_cards = {i for i, p in enumerate(picks) for q in players if name_similarity(p.get("name"), q.get("name")) >= 0.6}
    mate_legends += [p.get("legend", "") for i, p in enumerate(picks)
                     if p is not my_pick and not is_me(p.get("name")) and i not in on_cards and p.get("legend")]
    if len(picks or players) < 3:                 # legend select (or else the stats screen) says how many there were
        mate_legends += ["Empty"] * (2 - len(mate_legends))
    mate_legends = (mate_legends + [""] * 2)[:2]
    mode = stats.get("mode", "")
    when = datetime.fromisoformat(stats["ended"]) if stats.get("ended") else None
    entry = stats.get("dropship", {}).get("entry_rp") if mode == "Ranked" else None
    pct = percentile(stats.get("skill_graph", {})) if mode != "Ranked" else ""
    mate_names = [p.get("name") for p in (players or picks) if p is not me and p is not my_pick]
    # FRIENDS can be a dict (tag -> name for the column) or just a set of tags (the tag goes in the column)
    friend_names = FRIENDS if isinstance(FRIENDS, dict) else {tag: tag for tag in FRIENDS}
    friends = ",".join(friend_names[tag] for n in mate_names
                       for tag in [max(friend_names, key=lambda t: name_similarity(n, t), default=None)]
                       if tag and name_similarity(n, tag) >= 0.8)
    complete = me and all(me.get(k) is not None for k in ("kills", "assists", "knocks", "time", "damage", "revives", "respawns"))
    # No stats screen (requeued straight away): the last "SQUADS LEFT" seen stands in for placement
    placement = summary.get("placement") or stats.get("placement_seen") or location.get("squads_left_at_end") or ""
    # Ranked - Dropzone: no flight, the game names where the squad spawns ("DROPPING AT: BIG MAUDE")
    poi = drop_poi(stats) or (location.get("landing") or {}).get("poi", "")
    # Death Zone: where the squad's last survivor went down (you, or the teammate you were spectating)
    death_zone = (location.get("squad_out") or location.get("death") or {}).get("zone", "")
    contested = location.get("contested") or {}
    contested = ("Yes" if contested["contested"] else "No") if contested.get("feeds_read") else ""
    # Captures/<match>/fixes.json: your corrections win over anything read from the screen
    placement = fixes.get("placement", placement)
    poi = fixes.get("poi", poi)
    death_zone = fixes.get("death_zone", death_zone)
    contested = fixes.get("contested", contested)

    row = [""] * len(SHEET_HEADER)
    row[0] = placement
    row[1] = summary.get("squad_kills", "")
    if me:
        row[2], row[3], row[4] = me.get("kills", ""), me.get("assists", ""), me.get("knocks", "")
        row[5] = f"0:{int(me['time'].split(':')[0]):02d}:{me['time'].split(':')[1]}" if me.get("time") else ""
        row[6], row[7], row[8] = me.get("revives", ""), me.get("respawns", ""), me.get("damage", "")
        # Most Damage?: your real share of the squad's damage (a teammate who left counts as 0)
        team = sum(p["damage"] for p in players if p.get("damage") is not None)
        row[11] = round(me["damage"] / team, 4) if me.get("damage") is not None and team else ""
    row[9] = my_legend
    row[10] = f"{when.month}/{when.day}/{when.year} {when.hour}:{when:%M:%S}" if when else ""
    row[12] = death_zone
    row[14], row[15] = mode, stats.get("map", "")
    row[16] = friends
    row[17], row[18] = mate_legends[:2]
    row[19] = entry if entry is not None else ""
    row[20] = poi
    row[21] = contested
    row[22] = rank_distribution(stats) if mode == "Ranked" else ""
    row[24] = "" if stats.get("dropzone") else stats.get("dropship", {}).get("heading", "")   # no flight
    row[25] = est_xp(me, summary.get("placement")) if complete else ""
    row[26] = pct
    row[27] = skill(pct)
    if my_legend:
        row[28] = "-".join(sorted([row[9]] + mate_legends[:2]))

    match_cells = {
        "Match": stats["match"], "Time": row[10], "Game Mode": mode, "Map": stats.get("map", ""),
        "Placement": placement, "Squad Kills": row[1], "Entry": row[19], "Percentile": pct,
        "Dropship Heading": row[24], "Flight Path": "" if stats.get("dropzone") else location.get("dropship_heading") or "",
        "POI": poi, "Contested": contested,
        "Survey Scans": len((location.get("match_map") or {}).get("scans", [])) if location.get("match_map") else "",
        "Stats Screen": "Yes" if players else "Missed",
    }
    # Without a stats screen, still list who played what (from legend select)
    members = list(zip(players, player_legends)) or [(p, p.get("legend", "")) for p in picks]
    team_damage = sum(p.get("damage") or 0 for p, _ in members)
    squad_rows = []
    for i, (p, legend) in enumerate(members, 1):
        others = [l for q, l in members if q is not p] + ["Empty"] * (3 - len(members))
        mine = p is me or (not players and p is my_pick)
        damage = p.get("damage")
        squad_rows.append({**match_cells,
            "Death Zone": death_zone if mine else player_zone(p.get("name"), location),
            "Slot": i, "Player": p.get("name", ""), "Is Me": "Yes" if mine else "No", "Legend": legend,
            "Teammates": ", ".join(sorted(others)), "Squad": "-".join(sorted([legend] + others)),
            "Kills": p.get("kills", ""), "Assists": p.get("assists", ""), "Knocks": p.get("knocks", ""),
            "Damage": damage if damage is not None else "",
            "Damage %": round(damage / team_damage, 4) if damage is not None and team_damage else "",
            "Survival Time": p.get("time") or "", "Revives": p.get("revives", ""), "Respawns": p.get("respawns", "")})
    return row, squad_rows

# ---------------------------
# MAIN SCRIPT
# ---------------------------

CONFIG = load_config()

def extract_match(folder, finished=False):
    """Read one match. `finished`: a newer match has started, so this one is over even without a stats screen."""
    with open(folder / "match.json") as f:
        log = json.load(f)
    files = {}
    for e in log["events"]:
        if e.get("file") and e["event"] in ("first", "last", "page"):     # not the minimap / kill feed crops
            files.setdefault(e["screen"], []).append(e["file"])
    # read_at: when this read began - anything match.json gets after that (a stats screen still being saved while
    # this ran: 2026-10-31 19:43 lost its placement that way) means another read
    stats = {"version": STATS_VERSION, "match": folder.name, "started": log["started"], "warnings": [],
             "read_at": (folder / "match.json").stat().st_mtime}
    ended = next((e["time"] for e in log["events"] if e["screen"] == "post_game"), None)
    if ended:
        stats["ended"] = ended

    def latest(screen):
        names = files.get(screen, [])
        return cv2.imread(str(folder / names[-1])) if names else None

    picked = read_legend_select(folder, log["events"])
    if picked:
        stats["legend_select"] = picked
        for key in ("mode", "map", "dropzone", "drop_poi"):
            if key in stats["legend_select"]:
                stats[key] = stats["legend_select"].pop(key)
    image = latest("waiting")
    if image is not None:
        lobby = parse_mode_map(read_text(image[:260, :700]))
        for k, v in lobby.items():
            stats.setdefault(k, v)
        if shows_rank_distribution(image):
            stats["mode"] = "Ranked"                   # whatever the corner was caught as mid-animation
        # Only Trios / Duos lobbies have the skill graph (Ranked: rank distribution; Wildcard: how to play) - the
        # lobby's own mode / map then is what's read off it
        if stats.get("mode") in SKILL_GRAPH_MODES:
            stats["skill_graph"] = parse_skill_graph(image)
        elif stats.get("mode") == "Ranked":
            # Every lobby shot (the last can be mid-fade into legend select): the one that reads most believably
            shots = [(name, cv2.imread(str(folder / name))) for name in reversed(files.get("waiting", []))]
            name, stats["rank_distribution"] = best_rank_distribution(shots)
            if name:                                   # the bar + icons, kept to check the rank colours by
                stats["rank_distribution_from"] = name
                shot = dict(shots)[name]
                s = shot.shape[1] / CONFIG["base_size"][0]
                a, b, c, d = (round(v * s) for v in RANK_BAR_CROP)
                cv2.imwrite(str(folder / "rank_distribution.jpg"), shot[b:d, a:c], [cv2.IMWRITE_JPEG_QUALITY, 92])
    for name in files.get("dropship", []):   # first shot holds the starting heading
        stats["dropship"] = parse_dropship(cv2.imread(str(folder / name)))
        break
    # The compass while on the ship (kept every second for the first seconds of the match) beats a single
    # frame: the "IN DROPSHIP" banner isn't on screen the whole time
    steady = steady_heading(folder, log["events"])
    if steady is not None:
        stats.setdefault("dropship", {})["heading"] = steady
    # Following a jumpmaster there's no dropship screen to catch, but the compass and the entry cost
    # are still on the first frame of the match
    if not {"heading", "entry_rp"} <= set(stats.get("dropship", {})):
        # (only if the match was seen from the start: joining part-way, the compass is just where you face)
        from_start = any(e["screen"] in ("legend_select", "your_squad", "dropship") for e in log["events"])
        for name in files.get("in_game", [])[:1]:
            found = parse_dropship(cv2.imread(str(folder / name)))
            if not from_start:
                found.pop("heading", None)
            elif "heading" in found and "heading" not in stats.get("dropship", {}):
                found["heading_from"] = "first frame after the jump"
            stats["dropship"] = {**found, **stats.get("dropship", {})}

    best, best_score, header = None, -1, {}
    for name in files.get("post_game", []):
        image = cv2.imread(str(folder / name))
        experience = parse_experience(image)
        if experience:
            # The tab fills in a column at a time: keep what each shot adds (a later one has the placement)
            stats["experience"] = {**stats.get("experience", {}), **experience}
            continue
        summary = parse_summary(image)
        score = summary_score(summary)
        if score >= best_score and score > 0:   # ties go to the later screenshot
            best, best_score = dict(summary, file=name), score
        # The placement / total kills animate in after the cards, so take them from any shot that has them
        for key in ("placement", "squad_kills"):
            if summary and key in summary and not (key == "squad_kills" and summary.get("squad_kills_from_cards")):
                header[key] = summary[key]
    # Screens after the match we didn't recognise (a win's Champion screen / summary): try them too
    after = [e["file"] for e in log["events"] if e["event"] == "after_match"]
    for name in after:
        image = cv2.imread(str(folder / name))
        summary = parse_summary(image)
        if summary_score(summary) > max(best_score, 0):
            best, best_score = dict(summary, file=name), summary_score(summary)
        if "placement" not in header and any("CHAMPION" in squash(t) for _, _, t in read_text(image[:400])):
            header["placement"] = 1
    if header.get("placement"):
        stats["placement_seen"] = header["placement"]   # even without a readable summary (Champion screen)
    if "placement" not in header and stats.get("experience", {}).get("placement"):
        # The summary shot was taken too early, or has no placement (MATCH SUMMARY), or there's no summary at all
        header["placement"] = stats["experience"]["placement"]
        stats["placement_seen"] = header["placement"]
    picked_names = [p.get("name") for p in stats.get("legend_select", {}).get("squad", []) if p.get("name")]
    for player in (best or {}).get("players", []):
        close = player.pop("name_close", None)
        if close and picked_names and max(name_similarity(close, n) for n in picked_names) > \
                max(name_similarity(player.get("name") or "", n) for n in picked_names):
            player["name"] = close
    if best:
        best.update(header)
        if "squad_kills" in header:
            best.pop("squad_kills_from_cards", None)
    stats["summary"] = best

    if not best:
        stats["warnings"].append("no readable end-of-match summary")
    elif not any(is_me(p.get("name")) for p in best["players"]):
        stats["warnings"].append(f"couldn't find {PLAYER_NAME} on the summary")
    if best and best.get("squad_kills_from_cards"):
        stats["warnings"].append("squad kills added up from the cards (couldn't read the total)")
    if "legend_select" not in stats:
        stats["warnings"].append("no legend select screenshot")

    if (folder / "fixes.json").exists():
        stats["fixes"] = json.loads((folder / "fixes.json").read_text())

    stats["battle_royale_hud"] = any(e["screen"] == "in_game" for e in log["events"])
    # Mode / map typed into fixes.json win (a match whose lobby and legend select were missed)
    for key in ("mode", "map"):
        if stats.get("fixes", {}).get(key):
            stats[key] = stats["fixes"][key]
    # Wildcard's stats cards have Last Stands / Self Revives where the others have Revive / Respawn Given
    if not stats.get("mode") and best and any("last_stands" in p or "self_revives" in p for p in best["players"]):
        stats["mode"] = "Wildcard"
    # Joined part-way through (no legend select / lobby): which map, from the minimaps; Ranked if an
    # entry cost or ranked results were on screen
    if not stats.get("map") and tracked(stats):
        stats["map"] = guess_map(folder, log["events"])
        if stats["map"]:
            stats["warnings"].append(f"map worked out from the minimaps: {stats['map']}")
    if not stats.get("mode"):
        stats["warnings"].append("game mode not seen (capture joined part-way through) - type it in")
    if not stats.get("mode") and (stats.get("dropship", {}).get("entry_rp") or stats.get("experience")
                                  or shows_rank_badge(folder, log["events"])):
        stats["mode"] = "Ranked"

    # Over = reached the stats screen, or a newer match started (stats screen skipped by requeueing)
    stats["finished"] = bool(best) or finished
    if not stats.get("ended") and finished:
        played = [e["time"] for e in log["events"] if e["screen"] == "in_game"]
        if played:
            stats["ended"] = played[-1]
    if stats["finished"]:
        # Legend select missed (or one pick unread): the portraits on the squad panel say who played what
        picks = stats.get("legend_select", {}).get("squad", [])
        if sum(bool(p.get("legend")) for p in picks) < max(len((best or {}).get("players", [])), len(picks), 1):
            try:
                import Legend_Icons
                stats["panel_legends"] = Legend_Icons.squad_legends(folder, log["events"])
                if stats["panel_legends"]:
                    stats["warnings"].append("legends from the squad panel portraits: " + ", ".join(
                        f"{n} {l}" for n, l in stats["panel_legends"].items()))
            except Exception as e:
                stats["warnings"].append(f"squad panel legends: {e.__class__.__name__}: {e}")
        try:
            import Map_Locate
            if tracked(stats):                    # (Mixtape: no map positions, nothing learned about the maps)
                stats["location"] = Map_Locate.analyse_match(folder, stats)
        except Exception as e:
            stats["warnings"].append(f"map positions: {e.__class__.__name__}: {e}")
        if not best:
            stats["warnings"].append("stats screen missed; placement is the last SQUADS LEFT seen")

    with open(folder / "stats.json", "w") as f:
        json.dump(stats, f, indent=2)
    return stats

def read_legend_select(folder, events):
    """Mode, map and who picked what ("squad": slot, legend, name) from a match's legend select
    screenshots, or None if legend select wasn't seen. (Control_Panel also reads it mid-match.)"""
    names = [e["file"] for e in events
             if e["screen"] == "legend_select" and e.get("file") and e["event"] in ("first", "last", "page")]
    image = cv2.imread(str(folder / names[-1])) if names else None
    if image is None:
        return None
    found = parse_legend_select(image)
    # The last pick is often only shown after legend select stops being recognised: fill any slot
    # still missing a legend from the screenshots kept in between (newest first)
    reveals = [e["file"] for e in events if e["event"] == "pick_reveal"]
    squad = found.setdefault("squad", [])
    for name in reversed(reveals):
        for pick in parse_legend_select(cv2.imread(str(folder / name))).get("squad", []):
            known = next((p for p in squad if p["slot"] == pick["slot"]), None)
            if not known:
                squad.append(pick)
            elif not known.get("legend") and pick.get("legend"):
                known["legend"] = pick["legend"]
    squad.sort(key=lambda p: p["slot"])
    return found

def shows_rank_badge(folder, events):
    """Ranked shows your RP (e.g. "-48 RP") under the rank badge, top right, all match long."""
    frames = [e["file"] for e in events if e["event"] in ("full", "first") and e["screen"] in ("in_game", "spectate")]
    for name in frames[:4]:
        image = cv2.imread(str(folder / name))
        if image is None:
            continue
        s = image.shape[1] / 1920
        corner = image[: int(260 * s), int(1700 * s):]
        if any(re.search(r"\d\s*RP", t.upper()) for _, _, t in read_text(corner)):
            return True
    return False

def guess_map(folder, events, tries=12):
    """The map whose picture the most minimaps line up with (None if hardly any do)."""
    import Map_Locate
    shots = [e["file"] for e in events if e["event"] == "minimap" and e["screen"] in ("in_game", "spectate")]
    shots = shots[:: max(1, len(shots) // tries)][:tries]
    scores = {}
    for name in Map_Locate.MAP_NAMES:
        try:
            locator = Map_Locate.MapLocator(name)
        except FileNotFoundError:
            continue
        scores[name] = sum(bool(locator.locate(cv2.imread(str(folder / f)))) for f in shots)
    best = max(scores, key=scores.get) if scores else None
    return best if best and scores[best] >= max(3, len(shots) // 3) else None

def clean_up(folder, stats, min_age=KEEP_FOR_MINUTES):
    """Once a match has been read (at least `min_age` minutes ago), keep only what's needed (the stats, the
    timeline, the map picture / SVG / trails and one copy of the summary screen) and delete the rest of the
    screenshots."""
    if KEEP_SCREENSHOTS or not stats.get("finished") or not stats.get("location") or (folder / ".cleaned").exists():
        return
    if time.time() - (folder / "stats.json").stat().st_mtime < min_age * 60:
        return
    summary = (stats.get("summary") or {}).get("file")
    if summary and (folder / summary).exists():
        (folder / summary).replace(folder / "summary.jpg")
        stats["summary"]["file"] = "summary.jpg"
        with open(folder / "stats.json", "w") as f:
            json.dump(stats, f, indent=2)
    picture = folder / "match_map.png"
    if picture.exists():                           # the SVG has the detail; a JPEG is plenty to glance at
        cv2.imwrite(str(folder / "match_map.jpg"), cv2.imread(str(picture)), [cv2.IMWRITE_JPEG_QUALITY, 88])
    # The stats screen was seen but not read: keep its pages to find out why (2026-10-30 13:58 lost four
    # pages that way with nothing left to look at)
    unread = set() if stats.get("summary") else {p.name for p in folder.glob("*_post_game_*.jpg")}
    freed = 0
    for path in sorted(folder.rglob("*"), reverse=True):     # files first, then their (now empty) folders
        if path.is_file() and path.name not in KEPT_FILES and path.name not in unread:
            freed += path.stat().st_size
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    (folder / ".cleaned").touch()
    print(f"  {folder.name}: screenshots deleted ({freed / 1e6:.0f} MB)")

def needs_extract(folder, newest):
    out = folder / "stats.json"
    if (folder / ".cleaned").exists():
        return False                               # screenshots are gone: its stats.json is final
    if not out.exists():
        return True
    with open(out) as f:
        stats = json.load(f)
    if stats.get("version") != STATS_VERSION:
        return True
    if not stats.get("finished") and (folder != newest or gone_quiet(folder)):     # it's over now
        return True
    return (folder / "match.json").stat().st_mtime > stats.get("read_at", out.stat().st_mtime)

def gone_quiet(folder):
    """Nothing saved for OVER_AFTER_MINUTES: the match is over even though no newer one has started (the
    last game of a session, with no readable stats screen)."""
    return time.time() - (folder / "match.json").stat().st_mtime >= OVER_AFTER_MINUTES * 60

def work_due():
    """What a run would do now with no new match to set it off: read the newest match once it's gone quiet,
    or delete screenshots that have been kept long enough. Stat_Capture checks this every minute so the
    last games of a session don't wait for the next session. Returns a short description, or None."""
    folders = sorted(p.parent for p in CAPTURES_FOLDER.glob("*/match.json"))
    if not folders:
        return None
    newest = folders[-1]
    if gone_quiet(newest) and needs_extract(newest, newest):
        return f"read {newest.name}"
    for folder in folders:
        out = folder / "stats.json"
        if KEEP_SCREENSHOTS or (folder / ".cleaned").exists() or not out.exists() \
                or time.time() - out.stat().st_mtime < KEEP_FOR_MINUTES * 60:
            continue
        stats = json.loads(out.read_text())
        if stats.get("finished") and stats.get("location"):
            return f"clean up {folder.name}"
    return None

def fill_entry_costs(all_stats):
    """Ranked games where the entry cost wasn't on screen (no dropship shot): it only changes with your
    rank, so take it from the nearest ranked game in time that has it."""
    known = [(datetime.fromisoformat(s["ended"]), s["dropship"]["entry_rp"]) for s in all_stats
             if s.get("mode") == "Ranked" and s.get("ended") and s.get("dropship", {}).get("entry_rp")]
    for stats in all_stats:
        if stats.get("mode") != "Ranked" or not stats.get("ended") or stats.get("dropship", {}).get("entry_rp") or not known:
            continue
        when = datetime.fromisoformat(stats["ended"])
        stats.setdefault("dropship", {})["entry_rp"] = min(known, key=lambda k: abs((k[0] - when).total_seconds()))[1]
        stats["dropship"]["entry_rp_from"] = "nearest ranked game"
        with open(CAPTURES_FOLDER / stats["match"] / "stats.json", "w") as f:
            json.dump(stats, f, indent=2)

def write_sheets(all_stats):
    rows, squad_rows = [], []
    for stats in all_stats:
        if not tracked(stats):
            continue                               # Mixtape (Control / Gun Run / Team Deathmatch)
        row, squad = build_rows(stats)
        rows.append(row)
        squad_rows.extend(squad)
    with open(SHEET_FILE, "w", newline="") as f:
        csv.writer(f).writerows([SHEET_HEADER] + rows)
    with open(SQUAD_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, SQUAD_HEADER)
        writer.writeheader()
        writer.writerows(squad_rows)

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folders", nargs="*", help="match folders to (re)read; default: all new ones")
    parser.add_argument("--force", action="store_true", help="re-read every match")
    parser.add_argument("--no-sheet", action="store_true", help="don't add matches to the workbook (tests, old videos)")
    parser.add_argument("--clean", action="store_true", help="delete the screenshots of every match that's been read (now)")
    args = parser.parse_args()

    folders = sorted(p.parent for p in CAPTURES_FOLDER.glob("*/match.json"))
    newest = folders[-1] if folders else None
    todo = [Path(f) for f in args.folders] if args.folders else [f for f in folders if args.force or needs_extract(f, newest)]
    todo = [f for f in todo if not (f / ".cleaned").exists()]     # nothing left to read in those
    for folder in todo:
        stats = extract_match(folder, finished=folder.resolve() != newest.resolve() or gone_quiet(folder))
        summary = stats.get("summary") or {}
        print(f"{folder.name}: {stats.get('mode', '?')} {stats.get('map', '?')}, "
              f"#{summary.get('placement', '?')}, {len(summary.get('players', []))} players"
              + ("" if tracked(stats) else "  (not battle royale: not tracked)")
              + "".join(f"\n    ! {w}" for w in stats["warnings"]))

    all_stats = []
    for folder in folders:
        if not (folder / "stats.json").exists():
            continue                              # not read yet (a single folder was asked for)
        with open(folder / "stats.json") as f:
            stats = json.load(f)
        # Matches that reached the stats screen, or ended without a readable one but with a placement (EXPERIENCE
        # tab, Champion screen, or the last SQUADS LEFT)
        if stats.get("summary") or stats.get("placement_seen") or (stats.get("location") or {}).get("squads_left_at_end"):
            all_stats.append(stats)
    fill_entry_costs(all_stats)
    write_sheets(all_stats)
    print(f"Wrote {sum(map(tracked, all_stats))} matches to {SHEET_FILE.name} and {SQUAD_FILE.name}")

    # Straight into the LibreOffice workbook too
    if not args.no_sheet:
        import Sheet_Write
        try:
            Sheet_Write.write_pending(all_stats)
        except Exception as e:
            print(f"Couldn't add matches to {Sheet_Write.WORKBOOK.name} ({e.__class__.__name__}: {e}); will retry")
            Sheet_Write.PENDING_FLAG.touch()

    # The match is read and saved: its screenshots aren't needed any more (tests / old videos keep theirs)
    if args.clean or not args.no_sheet:
        for folder in folders:
            if (folder / "stats.json").exists():
                clean_up(folder, json.loads((folder / "stats.json").read_text()), 0 if args.clean else KEEP_FOR_MINUTES)

    # Refresh the all-games heatmap / ring history of the maps just played
    if not args.no_sheet:
        import Heatmap
        for name in sorted({json.loads((f / "stats.json").read_text()).get("map") for f in todo if (f / "stats.json").exists()} - {None}):
            try:
                Heatmap.heatmap(name), Heatmap.heatmap(name, only_me=True), Heatmap.ring_history(name)
            except Exception as e:
                print(f"Heatmap for {name} failed: {e.__class__.__name__}: {e}")

if __name__ == "__main__":
    sys.exit(main())
