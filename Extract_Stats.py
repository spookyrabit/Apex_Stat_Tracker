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
STATS_VERSION = 2              # bump to make old stats.json files get re-read
OCR_THREADS = 2

PLAYER_NAME = "Spookyrabit"    # your gamertag, without the [LIVE]/[BOT] club tag
FRIENDS = {}                   # gamertag -> what goes in the Friends column, e.g. {"SomeTag": "Lobster"}

LEGENDS = ["Alter", "Ash", "Axle", "Ballistic", "Bangalore", "Bloodhound", "Catalyst", "Caustic",
           "Conduit", "Crypto", "Fuse", "Gibraltar", "Horizon", "Lifeline", "Loba", "Mad Maggie",
           "Mirage", "New Castle", "Octane", "Pathfinder", "Rampart", "Revenant", "Seer", "Sparrow",
           "Valkyrie", "Vantage", "Wattson", "Wraith"]
MAPS = ["Worlds Edge", "Storm Point", "Broken Moon", "Olympus", "E-District", "Kings Canyon"]
MODES = ["Ranked", "Trios", "Duos", "Wildcard"]

# Skill graph -> "Percentile" column. The three numbers, rounded to 5s.
PERCENTILE_PARTS = ("start", "team", "end")

SHEET_HEADER = ["Placement", "Squad Kills", "Kills", "Assists", "Knocks", "Time", "Revives", "Respawns",
                "Damage", "Legend", "Time", "Most Damage?", "Death Zone", "", "Game Mode", "Map",
                "Freinds", "", "", "Entery", "POI", "Contested", "", "", "Dropship Heading", "EST XP",
                "Percentile", "Skill", "Squad", "Kills RP", "Placement RP", "Participation RP",
                "Loss Forgivness", "Ranked RP"]
# One row per player per match. Match-level columns repeat on each player's row so any row stands alone.
SQUAD_HEADER = ["Match", "Time", "Game Mode", "Map", "Placement", "Squad Kills", "Entry", "Percentile",
                "Dropship Heading", "Flight Path", "POI", "Death Zone", "Contested", "Stats Screen",
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

# Skill graph track in 1920x1080 pixels
GRAPH_TRACK = (660, 1260)
GRAPH_ROWS = (944, 1008)

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

def squad_names(stats):
    """Everyone's name in your squad, from the end cards and legend select."""
    names = [p.get("name") for p in (stats.get("summary") or {}).get("players", [])]
    names += [p.get("name") for p in stats.get("legend_select", {}).get("squad", [])]
    return sorted({n for n in names if n and len(squash(n)) >= 4})

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
    """Top-left "TRIOS / WORLD'S EDGE" on the waiting and legend select screens."""
    corner = sorted((y, t) for x, y, t in lines if x < 400 and y < 110)
    out = {}
    for _, text in corner:
        if "mode" not in out and best_match(text, MODES):
            out["mode"] = best_match(text, MODES)
        elif "map" not in out and best_match(text, MAPS):
            out["map"] = best_match(text, MAPS)
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
    return out

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
    entry = read_line(crop(image, scale_box([1780, 95, 1915, 135], CONFIG, image.shape[1])))
    m = re.search(r"-\s*(\d+)\s*RP", entry.upper().replace(" ", ""))
    if m:
        out["entry_rp"] = int(m.group(1))
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

def find_label(lines, word, x, y_min, y_max):
    for lx, ly, text in lines:
        if abs(lx - x) < 40 and y_min <= ly <= y_max and squash(word) in squash(text):
            return ly
    return None

def parse_summary(image):
    lines = read_text(image)
    if not any(squash(t).startswith("SQUAD") and "ELIMINATED" in squash(t) or "CHAMPION" in squash(t)
               for _, _, t in lines) and not any("PLACED" in squash(t) for _, _, t in lines):
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
    # One card per "Kills / Assists / Knocks" label; works for 2 or 3 cards
    for kx, ky, text in sorted(lines):
        if not squash(text).startswith("KILLSASSIST"):
            continue
        card = {"x": kx}
        for x, y, name in lines:
            if abs(x - kx) < 60 and ky - 110 < y < ky - 50:
                card["name"] = re.sub(r"^\[[^\]]*\]\s*", "", name)
        for key, (word, offset) in CARD_STATS.items():
            ly = find_label(lines, word, kx, ky + offset - 25, ky + offset + 25) or ky + offset
            card[key] = read_value(image, (kx - 4, ly + 18, kx + 110, ly + 52))
            # The close-up read can trip over the card art; the whole-screen read is the backup
            pattern = {"kak": r"\d+/\d+/\d+", "time": r"\d{1,2}:\d{2}"}.get(key, r"[\d,]+")
            if not re.fullmatch(pattern, card[key] or ""):
                below = [t.translate(DIGIT_FIXES) for x, y, t in lines if abs(x - kx) < 40 and ly + 12 <= y <= ly + 50]
                card[key] = next((t for t in below if re.fullmatch(pattern, t)), card[key])
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
    parts = sorted(int(5 * round(graph[k] / 5)) for k in PERCENTILE_PARTS)
    return "-".join(map(str, parts))

def skill(pct):
    if not pct:
        return ""
    a, b, c = map(int, pct.split("-"))
    return round((a * 2 + b + c * 4) / 7, 1)

def build_rows(stats):
    """Turn a stats.json dict into the Past Games row (list) and the per-player Squad Stats rows (dicts)."""
    summary = stats.get("summary") or {}
    location = stats.get("location") or {}
    players = summary.get("players", [])
    me = next((p for p in players if is_me(p.get("name"))), None)
    picks = stats.get("legend_select", {}).get("squad", [])
    my_pick = next((p for p in picks if is_me(p.get("name"))), None)
    # Teammates in end-card order (left card, then right), like the sheet; a missing teammate is "Empty"
    player_legends = assign_legends(players, picks)
    # Teammates as in your sheet: everyone who picked a legend (even if they left before the end), in
    # end-card order; "Empty" only for a slot nobody was in
    mate_legends = [l for p, l in zip(players, player_legends) if p is not me and l]
    on_cards = {i for i, p in enumerate(picks) for q in players if name_similarity(p.get("name"), q.get("name")) >= 0.6}
    mate_legends += [p.get("legend", "") for i, p in enumerate(picks)
                     if p is not my_pick and not is_me(p.get("name")) and i not in on_cards and p.get("legend")]
    if len(picks) < 3:                            # legend select says how many there were
        mate_legends += ["Empty"] * (2 - len(mate_legends))
    mate_legends = (mate_legends + [""] * 2)[:2]
    mode = stats.get("mode", "")
    when = datetime.fromisoformat(stats["ended"]) if stats.get("ended") else None
    entry = stats.get("dropship", {}).get("entry_rp") if mode == "Ranked" else None
    pct = percentile(stats.get("skill_graph", {})) if mode != "Ranked" else ""
    mate_names = [p.get("name") for p in (players or picks) if p is not me and p is not my_pick]
    friends = ",".join(FRIENDS[n] for n in mate_names if n in FRIENDS)
    complete = me and all(me.get(k) is not None for k in ("kills", "assists", "knocks", "time", "damage", "revives", "respawns"))
    # No stats screen (requeued straight away): the last "SQUADS LEFT" seen stands in for placement
    placement = summary.get("placement") or stats.get("placement_seen") or location.get("squads_left_at_end") or ""
    poi = (location.get("landing") or {}).get("poi", "")
    # Death Zone: where the squad's last survivor went down (you, or the teammate you were spectating)
    death_zone = (location.get("squad_out") or location.get("death") or {}).get("zone", "")
    contested = location.get("contested") or {}
    contested = ("Yes" if contested["contested"] else "No") if contested.get("feeds_read") else ""
    # Captures/<match>/fixes.json: your corrections win over anything read from the screen
    fixes = stats.get("fixes", {})
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
    row[9] = my_pick.get("legend", "") if my_pick else ""
    row[10] = f"{when.month}/{when.day}/{when.year} {when.hour}:{when:%M:%S}" if when else ""
    row[12] = death_zone
    row[14], row[15] = mode, stats.get("map", "")
    row[16] = friends
    row[17], row[18] = mate_legends[:2]
    row[19] = entry if entry is not None else ""
    row[20] = poi
    row[21] = contested
    row[24] = stats.get("dropship", {}).get("heading", "")
    row[25] = est_xp(me, summary.get("placement")) if complete else ""
    row[26] = pct
    row[27] = skill(pct)
    if my_pick:
        row[28] = "-".join(sorted([row[9]] + mate_legends[:2]))

    match_cells = {
        "Match": stats["match"], "Time": row[10], "Game Mode": mode, "Map": stats.get("map", ""),
        "Placement": placement, "Squad Kills": row[1], "Entry": row[19], "Percentile": pct,
        "Dropship Heading": row[24], "Flight Path": location.get("dropship_heading") or "",
        "POI": poi, "Contested": contested, "Stats Screen": "Yes" if players else "Missed",
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
            "Death Zone": death_zone if mine else "",
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
    stats = {"version": STATS_VERSION, "match": folder.name, "started": log["started"], "warnings": []}
    ended = next((e["time"] for e in log["events"] if e["screen"] == "post_game"), None)
    if ended:
        stats["ended"] = ended

    def latest(screen):
        names = files.get(screen, [])
        return cv2.imread(str(folder / names[-1])) if names else None

    image = latest("legend_select")
    if image is not None:
        stats["legend_select"] = parse_legend_select(image)
        for key in ("mode", "map"):
            if key in stats["legend_select"]:
                stats[key] = stats["legend_select"].pop(key)
        # The last pick is often only shown after legend select stops being recognised: fill any slot
        # still missing a legend from the screenshots kept in between (newest first)
        reveals = [e["file"] for e in log["events"] if e["event"] == "pick_reveal"]
        squad = stats["legend_select"].setdefault("squad", [])
        for name in reversed(reveals):
            for pick in parse_legend_select(cv2.imread(str(folder / name))).get("squad", []):
                known = next((p for p in squad if p["slot"] == pick["slot"]), None)
                if not known:
                    squad.append(pick)
                elif not known.get("legend"):
                    known["legend"] = pick["legend"]
        squad.sort(key=lambda p: p["slot"])
    image = latest("waiting")
    if image is not None:
        lobby = parse_mode_map(read_text(image[:260, :700]))
        for k, v in lobby.items():
            stats.setdefault(k, v)
        stats["skill_graph"] = parse_skill_graph(image)
    for name in files.get("dropship", []):   # first shot holds the starting heading
        stats["dropship"] = parse_dropship(cv2.imread(str(folder / name)))
        break
    # Following a jumpmaster there's no dropship screen to catch, but the compass and the entry cost
    # are still on the first frame of the match
    if not {"heading", "entry_rp"} <= set(stats.get("dropship", {})):
        for name in files.get("in_game", [])[:1]:
            found = parse_dropship(cv2.imread(str(folder / name)))
            stats["dropship"] = {**found, **stats.get("dropship", {})}

    best, best_score, header = None, -1, {}
    for name in files.get("post_game", []):
        image = cv2.imread(str(folder / name))
        experience = parse_experience(image)
        if experience:
            stats["experience"] = experience
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
    if best and "placement" not in header and stats.get("experience", {}).get("placement"):
        header["placement"] = stats["experience"]["placement"]     # the summary shot was taken too early
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

    # Over = reached the stats screen, or a newer match started (stats screen skipped by requeueing)
    stats["finished"] = bool(best) or finished
    if not stats.get("ended") and finished:
        played = [e["time"] for e in log["events"] if e["screen"] == "in_game"]
        if played:
            stats["ended"] = played[-1]
    if stats["finished"]:
        try:
            import Map_Locate
            stats["location"] = Map_Locate.analyse_match(folder, stats)
        except Exception as e:
            stats["warnings"].append(f"map positions: {e.__class__.__name__}: {e}")
        if not best:
            stats["warnings"].append("stats screen missed; placement is the last SQUADS LEFT seen")

    with open(folder / "stats.json", "w") as f:
        json.dump(stats, f, indent=2)
    return stats

def needs_extract(folder, newest):
    out = folder / "stats.json"
    if not out.exists():
        return True
    with open(out) as f:
        stats = json.load(f)
    if stats.get("version") != STATS_VERSION:
        return True
    if not stats.get("finished") and folder != newest:     # it's over now: a newer match started
        return True
    return (folder / "match.json").stat().st_mtime > out.stat().st_mtime

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
    args = parser.parse_args()

    folders = sorted(p.parent for p in CAPTURES_FOLDER.glob("*/match.json"))
    newest = folders[-1] if folders else None
    todo = [Path(f) for f in args.folders] if args.folders else [f for f in folders if args.force or needs_extract(f, newest)]
    for folder in todo:
        stats = extract_match(folder, finished=folder.resolve() != newest.resolve())
        summary = stats.get("summary") or {}
        print(f"{folder.name}: {stats.get('mode', '?')} {stats.get('map', '?')}, "
              f"#{summary.get('placement', '?')}, {len(summary.get('players', []))} players"
              + "".join(f"\n    ! {w}" for w in stats["warnings"]))

    all_stats = []
    for folder in folders:
        if not (folder / "stats.json").exists():
            continue                              # not read yet (a single folder was asked for)
        with open(folder / "stats.json") as f:
            stats = json.load(f)
        # Matches that reached the stats screen, or ended without it but with a placement from SQUADS LEFT
        if stats.get("summary") or (stats.get("location") or {}).get("squads_left_at_end"):
            all_stats.append(stats)
    fill_entry_costs(all_stats)
    write_sheets(all_stats)
    print(f"Wrote {len(all_stats)} matches to {SHEET_FILE.name} and {SQUAD_FILE.name}")

    # Straight into the LibreOffice workbook too
    if args.no_sheet:
        return
    import Sheet_Write
    try:
        Sheet_Write.write_pending(all_stats)
    except Exception as e:
        print(f"Couldn't add matches to {Sheet_Write.WORKBOOK.name} ({e.__class__.__name__}: {e}); will retry")
        Sheet_Write.PENDING_FLAG.touch()

if __name__ == "__main__":
    sys.exit(main())
