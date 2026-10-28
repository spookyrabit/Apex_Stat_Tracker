"""
A match's screenshots with what the readers measured drawn on them, for the window's Latest Match tab:

    lobby          the skill graph: the track, the shaded band's start / end and your team's red line (0-100),
                   and the Percentile / Skill they make; the mode / map in the corner
    legend_select  every OCR box (green = a legend, blue = a name, yellow = mode / map, grey = not used), the
                   three slots and the picks read (with the late pick reveals)
    dropship       the compass number and entry cost boxes (enlarged), and the compass read every second on the ship
    map            a full-map shot lined up with the map picture: the ring / next ring (with round and metres), the
                   dropship's flight line, scans and squad arrows found on it
    summary        the stats screen: each card's value boxes and what was read in them, placement, squad kills

Usage:
    .venv/bin/python Screen_Notes.py <match folder> <screen> <out.png> [screenshot] [map name]
(the screenshot defaults to the newest one of that screen; `sources()` lists them)
"""
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import Extract_Stats

# ---------------------------
# CONFIGURATION
# ---------------------------

# Colours (RGB)
GOOD = (90, 230, 90)          # read and used
READ = (110, 180, 255)        # a name / a value box
CORNER = (255, 210, 70)       # mode / map, measurements
IGNORED = (140, 140, 140)     # OCR text not used
RED = (255, 80, 80)
WHITE = (240, 240, 240)
PANEL = (0, 0, 0, 175)        # the notes panel over the screenshot

FULL_SHOTS = ("first", "last", "page", "pick_reveal", "full", "after_match")   # match.json events with whole frames
SCREENS = {"lobby": ("waiting",), "legend_select": ("legend_select",), "dropship": ("dropship",),
           "map": ("map",), "summary": ("post_game",)}


def sources(folder, events, stats=None):
    """{screen: screenshot path} for every screen this match has a screenshot of: the newest one (the dropship's
    first, which holds the starting heading; the stats screen the read picked)."""
    folder = Path(folder)
    out = {}
    for screen, names in SCREENS.items():
        shots = [e["file"] for e in events if e["screen"] in names and e.get("file") and e["event"] in FULL_SHOTS
                 and e["event"] != "pick_reveal" and (folder / e["file"]).exists()]
        if shots:
            out[screen] = folder / (shots[0] if screen == "dropship" else shots[-1])
    picked = ((stats or {}).get("summary") or {}).get("file")
    if picked and (folder / picked).exists():
        out["summary"] = folder / picked
    return out


# ---------------------------
# DRAWING
# ---------------------------

class Sheet:
    """A 1080p screenshot to draw notes on, plus a dark panel of enlarged close-ups and text lines over one part
    of it (`panel`: x1, y1, x2 of where it goes; it grows down as needed)."""

    def __init__(self, bgr, panel=(460, 10, 1460)):
        if bgr.shape[1] != 1920:
            bgr = cv2.resize(bgr, (1920, round(1920 * bgr.shape[0] / bgr.shape[1])), interpolation=cv2.INTER_AREA)
        self.bgr = bgr
        self.image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
        self.draw = ImageDraw.Draw(self.image)
        self.panel, self.close_ups, self.notes = panel, [], []

    def font(self, size):
        import Match_Map
        return Match_Map.font(size)

    def box(self, x1, y1, x2, y2, colour, label=None, width=2, above=True):
        self.draw.rectangle((x1, y1, x2, y2), outline=colour, width=width)
        if label:
            self.text(x1, y1 - 24 if above else y2 + 3, label, colour, 19)

    def polygon(self, points, colour, label=None):
        pts = [tuple(map(float, p)) for p in points]
        self.draw.polygon(pts, outline=colour, width=2)
        if label:
            self.text(pts[0][0], pts[0][1] - 22, label, colour, 18)

    def line(self, a, b, colour, width=3):
        self.draw.line((tuple(a), tuple(b)), fill=colour, width=width)

    def circle(self, x, y, r, colour, label=None, width=3):
        self.draw.ellipse((x - r, y - r, x + r, y + r), outline=colour, width=width)
        if label:
            self.text(x - r * 0.7, y - r - 26, label, colour, 20)

    def text(self, x, y, s, colour=WHITE, size=20):
        self.draw.text((x, y), s, fill=colour, font=self.font(size), stroke_width=3, stroke_fill=(0, 0, 0))

    def close_up(self, box, zoom, title):
        """An enlarged copy of this part of the screenshot (taken when saving, so it shows what's drawn on it)."""
        self.close_ups.append((box, zoom, title))

    def note(self, s, colour=WHITE):
        self.notes.append((s, colour))

    def save(self, path):
        x1, y1, x2 = self.panel
        pieces = []
        for (a, b, c, d), zoom, title in self.close_ups:
            crop = self.image.crop((a, b, c, d))
            pieces.append((crop.resize((int(crop.width * zoom), int(crop.height * zoom)), Image.LANCZOS), title))
        width = x2 - x1
        y = 12
        height = 12 + sum(p.height + 32 for p, _ in pieces) + 30 * len(self.notes) + 10
        overlay = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle((x1, y1, x2, y1 + height), fill=PANEL)
        out = Image.alpha_composite(self.image, overlay)
        draw = ImageDraw.Draw(out)
        for piece, title in pieces:
            if piece.width > width - 20:
                piece = piece.resize((width - 20, int(piece.height * (width - 20) / piece.width)), Image.LANCZOS)
            draw.text((x1 + 10, y1 + y), title, fill=CORNER, font=self.font(19), stroke_width=2, stroke_fill=(0, 0, 0))
            out.paste(piece, (x1 + 10, y1 + y + 26))
            y += piece.height + 32
        for s, colour in self.notes:
            draw.text((x1 + 12, y1 + y), s, fill=colour, font=self.font(20), stroke_width=2, stroke_fill=(0, 0, 0))
            y += 30
        out.convert("RGB").save(path)


def ocr_boxes(image):
    """Full OCR with each text's box: [(4 corner points, text)]."""
    result, _ = Extract_Stats.ocr_engine()(image)
    return [(np.array(box, float), text) for box, text, _ in (result or [])]


def mode_map_corner(sheet, boxes):
    """Yellow boxes on the top-left mode / map text (parse_mode_map) with what it was read as."""
    found = {}
    for box, text in boxes:
        x, y = box[0]
        if x < 400 and y < 110:
            mode, map_name = Extract_Stats.best_match(text, Extract_Stats.MODES), Extract_Stats.best_match(text, Extract_Stats.MAPS)
            label = mode or map_name
            sheet.polygon(box, CORNER if label else IGNORED)
            if label:
                sheet.text(max(box[:, 0]) + 8, y, label, CORNER, 18)     # beside it: the corner is crowded
                found["mode" if mode else "map"] = label
    return found


# ---------------------------
# SCREENS
# ---------------------------

def lobby(image, stats, folder, events):
    sheet = Sheet(image)
    img = sheet.bgr
    (x1, x2), (y1, y2) = Extract_Stats.GRAPH_TRACK, Extract_Stats.GRAPH_ROWS
    graph = Extract_Stats.parse_skill_graph(img)
    sheet.box(x1, y1, x2, y2, CORNER)
    for pct in (0, 25, 50, 75, 100):
        x = x1 + (x2 - x1) * pct / 100
        sheet.line((x, y2 + 2), (x, y2 + 14), CORNER, 2)
        sheet.text(x - 12, y2 + 14, str(pct), CORNER, 16)
    # (start label left of its line, end label right of it, your team above both: they're often close together)
    for key, colour, label, dx, dy in (("start", GOOD, "band start", -1, 58), ("end", GOOD, "band end", 1, 58),
                                       ("team", RED, "your team", 0, 84)):
        if key in graph:
            x = x1 + (x2 - x1) * graph[key] / 100
            sheet.line((x, y1 - 30), (x, y2 + 4), colour, 3)
            text = f"{label} {graph[key]}"
            width = sheet.draw.textlength(text, font=sheet.font(18))
            sheet.text(x - width - 6 if dx < 0 else x + 6 if dx > 0 else x - width / 2, y1 - dy, text, colour, 18)
    sheet.close_up((x1 - 160, y1 - 95, x2 + 160, y2 + 40), 1.05, "Skill graph (enlarged)")
    corner = mode_map_corner(sheet, ocr_boxes(img[:260, :700]))
    if graph:
        pct = Extract_Stats.percentile(graph)
        sheet.note(f"Shaded band {graph['start']} to {graph['end']}, your team {graph['team']} (0-100 along the track)")
        if pct:
            a, b, c = pct.split("-")
            sheet.note(f"Percentile {pct} (each rounded to 5)  ->  Skill (2x{a} + {b} + 4x{c}) / 7 = {Extract_Stats.skill(pct)}", GOOD)
    else:
        sheet.note("No skill graph on this screenshot (no red team line / shaded band on the track)", RED)
    sheet.note(f"Mode {corner.get('mode', 'not read')} (top-left corner; the map is taken from legend select)", CORNER)
    return sheet


def legend_select(image, stats, folder, events):
    sheet = Sheet(image, panel=(470, 120, 1450))
    img = sheet.bgr
    boxes = ocr_boxes(img)
    for x in (640, 1280):
        sheet.line((x, 990), (x, 1080), CORNER, 2)
    sheet.line((0, 990), (1920, 990), CORNER, 1)
    sheet.line((0, 1030), (1920, 1030), CORNER, 1)
    sheet.text(8, 960, "slots: thirds of the bar · legends above the line, names below", CORNER, 17)
    corner = mode_map_corner(sheet, boxes)
    for box, text in boxes:
        x, y = box[0]
        if y < 990:
            if not (x < 400 and y < 110):
                sheet.polygon(box, IGNORED)
            continue
        legend = Extract_Stats.best_match(text, Extract_Stats.LEGENDS, cutoff=0.75)
        if y < 1030 and legend:
            sheet.polygon(box, GOOD)
        elif y >= 1030:
            sheet.polygon(box, READ)
        else:
            sheet.polygon(box, IGNORED)
    picked = Extract_Stats.read_legend_select(Path(folder), events) or {}
    reveals = sum(e["event"] == "pick_reveal" for e in events)
    sheet.note(f"Mode {picked.get('mode') or corner.get('mode', '?')}  ·  map {picked.get('map') or corner.get('map', '?')}", CORNER)
    fixed = Extract_Stats.load_fixes(stats).get("legends", {}) if stats.get("match") else {}
    for p in picked.get("squad", []):
        name = p.get("name") or "?"
        you = "  (you)" if Extract_Stats.is_me(name) else ""
        legend = p.get("legend") or "not picked / not read"
        extra = f"  ->  typed in: {fixed[name]}" if fixed.get(name) else ""
        sheet.note(f"Slot {p['slot']}: {legend}  ·  {name}{you}{extra}", GOOD if p.get("legend") else RED)
    if not picked.get("squad"):
        sheet.note("Nobody read on the bottom bar", RED)
    sheet.note(f"Green = legend, blue = name, grey = not used; {reveals} pick reveal shots fill slots read empty here", IGNORED)
    return sheet


def dropship(image, stats, folder, events):
    sheet = Sheet(image, panel=(10, 440, 900))
    img = sheet.bgr
    read = Extract_Stats.parse_dropship(img)
    sheet.box(925, 84, 995, 124, GOOD if "heading" in read else RED, str(read.get("heading", "?")), above=False)
    sheet.box(1780, 95, 1915, 135, GOOD if "entry_rp" in read else IGNORED, f"entry {read.get('entry_rp', '-')}", above=False)
    sheet.close_up((860, 50, 1060, 160), 1.5, "Compass (between the chevrons)")
    sheet.close_up((1700, 70, 1920, 160), 1.5, "Entry cost (ranked)")
    readings = [(e["time"][11:19], Extract_Stats.read_compass(cv2.imread(str(Path(folder) / e["file"]))))
                for e in events if e["event"] == "compass" and (Path(folder) / e["file"]).exists()][:16]
    sheet.note(f"This frame: heading {read.get('heading', 'not read')}  ·  entry cost {read.get('entry_rp', 'none (not ranked)')}")
    if readings:
        sheet.note("Compass every second: " + " ".join(str(r) if r is not None else "?" for _, r in readings[:16]), READ)
        steady = Extract_Stats.steady_heading(Path(folder), events)
        sheet.note(f"First reading held 3 s: {steady if steady is not None else 'none'}", GOOD if steady is not None else RED)
    used = (stats.get("dropship") or {}).get("heading")
    if used is not None:
        sheet.note(f"Dropship heading used: {used}" + (f" ({stats['dropship']['heading_from']})" if stats["dropship"].get("heading_from") else ""), GOOD)
    return sheet


def map_screen(image, stats, folder, events, map_name=None):
    import Map_Locate
    import Match_Map
    sheet = Sheet(image, panel=(8, 300, 450))
    shot = sheet.bgr
    x1, y1, x2, y2 = Match_Map.MAP_VIEW_BOX
    view = shot[y1:y2, x1:x2]
    names = [map_name or stats.get("map")] if (map_name or stats.get("map")) else Map_Locate.MAP_NAMES
    best = None
    for name in names:
        try:
            locator = Map_Locate.MapLocator(name)
        except FileNotFoundError:
            continue
        where = locator.locate_image(view)
        if where and (best is None or where["inliers"] > best[1]["inliers"]):
            best = (locator, where)
    sheet.box(x1, y1, x2, y2, IGNORED, width=1)
    if not best:
        sheet.note("Couldn't line this shot up with a map picture", RED)
        return sheet
    locator, where = best
    back = cv2.invertAffineTransform(where["fit"])
    to_shot = lambda x, y: tuple(back @ np.array([x, y, 1.0]) + [x1, y1])
    sheet.note(f"{locator.map_name}: lined up ({where['inliers']} matches, x{1 / where['scale']:.2f} zoom)")
    for ring in Match_Map.rings_from_map_screen(locator, shot):
        x, y = to_shot(ring["x"], ring["y"])
        r = ring["r"] / where["scale"]
        rnd, metres = locator.ring_round(ring["r"])
        size = f"round {rnd}, {metres} m" if rnd else (f"{metres} m, not a ring size" if metres else f"{ring['r']:.0f} px")
        colour = WHITE if ring["kind"] == "ring" else READ
        sheet.circle(x, y, r, colour, f"{ring['kind']} · {size}")
        sheet.note(f"{ring['kind'].capitalize()}: {size}" + (" (faint: needs a 2nd shot)" if ring.get("faint") else "")
                   + (" (partly on screen)" if ring.get("partial") else ""), colour)
    flight = Match_Map.flight_from_map_screen(locator, shot)
    if flight:
        a, b = to_shot(flight["x1"], flight["y1"]), to_shot(flight["x2"], flight["y2"])
        sheet.line(a, b, CORNER, 4)
        heading = np.degrees(np.arctan2(flight["x2"] - flight["x1"], -(flight["y2"] - flight["y1"]))) % 180
        sheet.note(f"Dropship flight line: {heading:.0f} / {heading + 180:.0f} deg", CORNER)
    for scan in Match_Map.scans_from_map_screen(locator, shot):
        x, y = to_shot(scan["x"], scan["y"])
        sheet.circle(x, y, scan["r"] / where["scale"], (60, 220, 200), "scan", 2)
        sheet.note("Survey beacon scan", (60, 220, 200))
    arrows = Match_Map.squad_on_map_screen(locator, shot)
    for colour, spots in arrows.items():
        for sx, sy in spots:
            x, y = to_shot(sx, sy)
            sheet.circle(x, y, 14, GOOD, colour, 2)
    if arrows:
        sheet.note("Squad arrows: " + ", ".join(f"{c} x{len(s)}" for c, s in arrows.items()), GOOD)
    return sheet


def summary(image, stats, folder, events):
    sheet = Sheet(image, panel=(10, 780, 1910))
    img = sheet.bgr
    boxes = ocr_boxes(img)
    lines = [(int(b[0][0]), int(b[0][1]), t) for b, t in boxes]
    read = Extract_Stats.parse_summary(img) or {}
    cards = sorted((x, y) for x, y, t in lines if Extract_Stats.squash(t).startswith("KILLSASSIST"))
    players = sorted(read.get("players", []), key=lambda p: p["x"])
    for box, text in boxes:
        if Extract_Stats.squash(text).startswith("KILLSASSIST") or Extract_Stats.squash(text) in ("TOTALKILLS", "WITHSQUAD", "SQUAD"):
            sheet.polygon(box, CORNER)
        elif re.fullmatch(r"#\s*\d{1,2}", text.strip()):
            sheet.polygon(box, GOOD, f"placement {read.get('placement', '?')}")
    for (kx, ky), p in zip(cards, players):
        values = {"kak": f"{p.get('kills')}/{p.get('assists')}/{p.get('knocks')}", "damage": p.get("damage"),
                  "time": p.get("time"), "revives": p.get("revives"), "respawns": p.get("respawns")}
        for key, (word, offset) in Extract_Stats.CARD_STATS.items():
            ly = Extract_Stats.find_label(lines, word, kx, ky + offset - 25, ky + offset + 25) or ky + offset
            ok = values[key] not in (None, "None/None/None")
            sheet.box(kx - 4, ly + 18, kx + 110, ly + 52, GOOD if ok else RED)
            sheet.text(kx + 116, ly + 22, str(values[key]), GOOD if ok else RED, 19)
    sheet.note(f"Placement #{read.get('placement', '?')}  ·  squad kills {read.get('squad_kills', '?')}"
               + ("  (added up from the cards)" if read.get("squad_kills_from_cards") else ""), GOOD)
    for p in players:
        sheet.note(f"{p.get('name', '?')}: {p.get('kills')}/{p.get('assists')}/{p.get('knocks')} K/A/Kn  ·  {p.get('damage')} damage  ·  "
                   f"survived {p.get('time')}  ·  revives {p.get('revives')}  ·  respawns {p.get('respawns')}", READ)
    if not players:
        sheet.note("No player cards read on this screenshot", RED)
    return sheet


NOTES = {"lobby": lobby, "legend_select": legend_select, "dropship": dropship, "map": map_screen, "summary": summary}


def make(folder, screen, out, shot=None, map_name=None):
    folder = Path(folder)
    events = json.loads((folder / "match.json").read_text())["events"]
    stats = json.loads((folder / "stats.json").read_text()) if (folder / "stats.json").exists() else {}
    shot = Path(shot) if shot else sources(folder, events, stats).get(screen)
    if not shot:
        raise SystemExit(f"No {screen} screenshot in {folder.name}")
    image = cv2.imread(str(shot))
    kwargs = {"map_name": map_name} if screen == "map" else {}
    sheet = NOTES[screen](image, stats, folder, events, **kwargs)
    sheet.note(f"{shot.name}", IGNORED)
    sheet.save(out)


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[2] not in NOTES:
        raise SystemExit(__doc__)
    make(*sys.argv[1:6])
