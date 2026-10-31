"""
The tracker's window: your stats and the capture controls in one place.

Filters along the top (mode, map, legend, time) apply to every tab but the first:
    Latest    the newest match: the live map big, and underneath who's playing what, then their stats
    Overview  totals and averages, best games, and a breakdown by legend / map / POI / squad / friends...
    Squad     the same for every player in your squads (Squad Stats): you vs teammates, friends, randoms
    Matches   every game in the workbook; pick one to see its squad, match picture and stats screen
    Maps      each map's heatmap (everyone / just you) and where the endgame rings ended up
    Capture   is the capture running, restart / stop it, the workbook commands and the live capture.log

Stats are read straight from the workbook (read-only, fine while it's open in Calc) and re-read whenever it
changes, so a match shows up a minute or so after it's written.

Usage:
    .venv/bin/python Control_Panel.py
"""
import json
import queue
import re
import shutil
import subprocess
import threading
import zlib
import tkinter as tk
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import Extract_Stats
import Screen_Notes

# ---------------------------
# CONFIGURATION
# ---------------------------

HERE = Path(__file__).parent
PYTHON = HERE / ".venv" / "bin" / "python"
CAPTURES = HERE / "Captures"
LOG = CAPTURES / "capture.log"
WORKBOOK = HERE / "Apex Game Data S30.ods"
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
                "T": "entry", "U": "poi", "V": "contested", "W": "rank_distribution", "AH": "rp"}
# Pictures on the Maps tab: file in Maps/<Map>/, button, what it shows
MAP_PICTURES = [("heatmap.png", "Everyone's routes",
                 "How many player-trips passed each spot (your whole squad): blue = sometimes → red = most often. "
                 "Green dots = landings."),
                ("heatmap_me.png", "My routes", "The same, for your own trails only."),
                ("rings.png", "Endgame rings",
                 "Where endgames ended up (the last ring sizes). A number = how many games ended in that circle.")]
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


def read_workbook(path=WORKBOOK):
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


def load_stats():
    """Every game in Past Games as a dict (newest first), each with its squad from Squad Stats when the
    match was captured."""
    tabs = read_workbook()
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
        if not g["match"]:              # hand-typed rows used L for a 0 / 1 / 2 mark, not a share of damage
            g["damage_share"] = None
        games.append(g)
    games.sort(key=lambda g: g["time"], reverse=True)
    name_players(games)
    return games


def name_players(games):
    """Give every Squad Stats row a steady player name ("who": "You", or the commonest spelling of that tag;
    the end card cuts names off and OCR slips letters, so "nutzenhar" is "nutzenhammer"), who else was in
    the squad ("with"), whether they're a friend, and whether they did the most damage in the squad ("top")."""
    spellings = Counter(p["Player"] for g in games for p in g["squad"] if p.get("Is Me") != "Yes" and p.get("Player"))
    groups = []                     # spellings of one tag, commonest first
    for name, _ in spellings.most_common():
        group = next((g for g in groups if any(Extract_Stats.name_similarity(name, n) >= 0.8 for n in g)), None)
        group.append(name) if group else groups.append([name])
    steady = {name: g[0] for g in groups for name in g}
    friends = list(Extract_Stats.FRIENDS)
    for g in games:
        top = max((p.get("Damage") or 0 for p in g["squad"]), default=0)
        for p in g["squad"]:
            p["who"] = "You" if p.get("Is Me") == "Yes" else steady.get(p.get("Player"), "(no name)")
            p["friend"] = p["who"] != "You" and any(Extract_Stats.name_similarity(p["who"], f) >= 0.8 for f in friends)
            p["top"] = bool(top) and (p.get("Damage") or 0) == top
        for p in g["squad"]:
            p["with"] = [q["who"] for q in g["squad"] if q is not p]


# ---------------------------
# NUMBERS
# ---------------------------

def mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def fmt_num(value, digits=0):
    return "–" if value is None else f"{value:,.{digits}f}"


def fmt_pct(value):
    return "–" if value is None else f"{value:.0%}"


def fmt_time(seconds):
    return "–" if seconds is None else f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def fmt_when(when):
    return when.strftime("%a %d %b %H:%M") if when else ""


def is_ranked(g):
    return (g["mode"] or "").lower() == "ranked"


def summary(games):
    """Totals and per-game averages for a list of games."""
    n = len(games)
    ranked = [g for g in games if is_ranked(g)]
    known_contest = [g for g in games if g["contested"] in ("Yes", "No")]
    return dict(
        games=n,
        wins=sum(g["placement"] == 1 for g in games),
        win_rate=sum(g["placement"] == 1 for g in games) / n if n else None,
        top5=sum(g["placement"] <= 5 for g in games) / n if n else None,
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
    )


# what the Overview's breakdown can group games by
GROUPS = {
    "Legend": lambda g: [g["legend"]],
    "Map": lambda g: [g["map"]],
    "Game mode": lambda g: [g["mode"]],
    "Landing (POI)": lambda g: [g["poi"]],
    "Death zone": lambda g: [g["death_zone"]],
    "Teammate legend": lambda g: g["mates"] or ["(solo)"],
    "Squad (all 3 legends)": lambda g: [" + ".join(sorted([g["legend"] or "?"] + g["mates"]))],
    "Friends": lambda g: [f.strip() for f in g["friends"].split(",")] if g["friends"] else ["(randoms)"],
    "Weekday": lambda g: [g["time"].strftime("%A")],
    "Hour of day": lambda g: [g["time"].strftime("%H:00")],
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
            rows = sorted(known, key=lambda r: r[self.sort_key], reverse=self.reverse) + \
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
    ]

    def __init__(self, master):
        super().__init__(master, padding=10)
        tiles = ttk.Frame(self)
        tiles.pack(fill="x")
        self.tiles = []
        for i, (name, *_) in enumerate(self.TILES + [("Best damage",), ("Most kills",)]):
            box = ttk.Frame(tiles, style="Tile.TFrame", padding=(10, 6))
            box.grid(row=i // 4, column=i % 4, sticky="nsew", padx=3, pady=3)
            ttk.Label(box, text=name, style="TileName.TLabel").pack(anchor="w")
            value = ttk.Label(box, text="–", style="TileValue.TLabel")
            value.pack(anchor="w")
            note = ttk.Label(box, text="", style="TileNote.TLabel")
            note.pack(anchor="w")
            self.tiles.append((value, note))
        for c in range(4):
            tiles.columnconfigure(c, weight=1, uniform="tile")

        breakdown = ttk.LabelFrame(self, text="Breakdown", padding=6)
        breakdown.pack(fill="both", expand=True, pady=(10, 0))
        top = ttk.Frame(breakdown)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Group by:").pack(side="left")
        self.group = ttk.Combobox(top, values=list(GROUPS), state="readonly", width=22)
        self.group.set("Legend")
        self.group.pack(side="left", padx=6)
        self.group.bind("<<ComboboxSelected>>", lambda e: self.show_breakdown())
        ttk.Label(top, text="click a heading to sort", foreground=FAINT).pack(side="right")
        self.table = Table(breakdown, [
            ("name", "Group", 200, text, "w"), ("games", "Games", 70, fmt_num, "e"),
            ("wins", "Wins", 60, fmt_num, "e"), ("win_rate", "Win %", 65, fmt_pct, "e"),
            ("top5", "Top 5 %", 70, fmt_pct, "e"), ("placement", "Avg place", 80, lambda v: fmt_num(v, 1), "e"),
            ("kills", "Kills", 60, lambda v: fmt_num(v, 2), "e"), ("knocks", "Knocks", 65, lambda v: fmt_num(v, 2), "e"),
            ("damage", "Damage", 75, fmt_num, "e"), ("survival", "Survival", 75, fmt_time, "e"),
            ("rp_per_game", "RP / ranked", 90, lambda v: fmt_num(v, 1), "e")])
        self.table.pack(fill="both", expand=True)
        self.games = []

    def show(self, games):
        self.games = games
        s = summary(games)
        for (value, note), (_, get_value, get_note) in zip(self.tiles, self.TILES):
            value.configure(text=get_value(s) if games else "–")
            note.configure(text=get_note(s) if games else "")
        for (value, note), key in zip(self.tiles[len(self.TILES):], ("damage", "kills")):
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
        for i, name in enumerate([t[0] for t in self.TILES] + self.EXTRA):
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
            ("respawns", "Respawns", 70, lambda v: fmt_num(v, 2), "e")])
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
        for (value, note), (_, get_value, get_note) in zip(self.tiles, self.TILES):
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
        if not chosen:
            self.destroy()
            return
        if (HERE / f".~lock.{WORKBOOK.name}#").exists():
            messagebox.showinfo("Workbook open", f"Close {WORKBOOK.name} in Calc first, then Save again.", parent=self)
            return
        path = self.folder / "fixes.json"
        fixes = json.loads(path.read_text()) if path.exists() else {}
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
            ("time", "When", 130, fmt_when, "w"), ("mode", "Mode", 70, text, "w"), ("map", "Map", 100, text, "w"),
            ("placement", "Place", 55, fmt_num, "e"), ("kills", "Kills", 50, fmt_num, "e"),
            ("assists", "Assists", 60, fmt_num, "e"), ("knocks", "Knocks", 60, fmt_num, "e"),
            ("damage", "Damage", 70, fmt_num, "e"), ("damage_share", "Dmg %", 60, fmt_pct, "e"),
            ("survival", "Survival", 65, fmt_time, "e"), ("legend", "Legend", 95, text, "w"),
            ("mates_text", "Teammates", 150, text, "w"), ("friends", "Friends", 90, text, "w"),
            ("poi", "Landed", 120, text, "w"), ("contested", "Hot drop", 70, text, "w"),
            ("death_zone", "Died at", 120, text, "w"), ("rp", "RP", 55, lambda v: fmt_num(v), "e")],
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
            ("Kills", "Kills", 50, fmt_num, "e"), ("Assists", "Assists", 60, fmt_num, "e"),
            ("Knocks", "Knocks", 60, fmt_num, "e"), ("Damage", "Damage", 70, fmt_num, "e"),
            ("Damage %", "Dmg %", 60, fmt_pct, "e"), ("Survival Time", "Survival", 70, fmt_time, "e"),
            ("Revives", "Revives", 60, fmt_num, "e"), ("Respawns", "Respawns", 70, fmt_num, "e"),
            ("Death Zone", "Died at", 120, text, "w")], height=3)
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
                f"{fmt_num(game['rp'])} RP" if is_ranked(game) and game["rp"] is not None else "",
                f"lobby: {game['rank_distribution']}" if game["rank_distribution"] else ""]
        if not game["match"]:
            bits.append("typed in by hand: no capture for this match")
        self.subheading.configure(text="  ·  ".join(b for b in bits if b))
        self.squad.show([dict(p, Player=(p.get("Player") or "?") + ("  (you)" if p.get("Is Me") == "Yes" else ""))
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

        right = ttk.Frame(self, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(right)
        top.pack(fill="x")
        self.picture_name = tk.StringVar(value=MAP_PICTURES[0][0])
        for file, label, _ in MAP_PICTURES:
            ttk.Radiobutton(top, text=label, value=file, variable=self.picture_name, style="Toolbutton",
                            command=self.refresh).pack(side="left", padx=(0, 4))
        ttk.Label(top, text="click the picture to open it full size", foreground=FAINT).pack(side="right")
        self.about = ttk.Label(right, text="", foreground=MUTED, wraplength=700, justify="left")
        self.about.pack(fill="x", pady=(6, 0))
        self.stats = ttk.Label(right, text="", font=("TkDefaultFont", 10, "bold"), wraplength=700, justify="left")
        self.stats.pack(fill="x", pady=(2, 6))
        self.picture = FittedImage(right)
        self.picture.pack(fill="both", expand=True)
        self.games, self.mtimes = [], {}

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
        file = self.picture_name.get()
        path = MAPS / name / file
        self.about.configure(text=next(about for f, _, about in MAP_PICTURES if f == file))
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
        mtime = path.stat().st_mtime if path.exists() else None
        if force or self.mtimes.get(path) != mtime:
            self.mtimes[path] = mtime
            self.picture.set(path, missing=f"No {file} for {name} yet: it's drawn after a captured match on this map.")

    def poll(self):
        """Heatmaps are redrawn after every match: pick up a new one."""
        self.refresh(force=False)


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
# Mid-match, legend select is read in its own low-priority process: OCR shouldn't slow the game or the window
READ_PICKS = ("import json, sys; from pathlib import Path; import Extract_Stats; folder = Path(sys.argv[1]); "
              "events = json.loads((folder / 'match.json').read_text())['events']; "
              "print(json.dumps(Extract_Stats.read_legend_select(folder, events)))")


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
            ("Kills", "Kills", 50, fmt_num, "e"), ("Assists", "Assists", 60, fmt_num, "e"),
            ("Knocks", "Knocks", 60, fmt_num, "e"), ("Damage", "Damage", 70, fmt_num, "e"),
            ("Damage %", "Dmg %", 60, fmt_pct, "e"), ("Survival Time", "Survival", 70, fmt_time, "e"),
            ("Revives", "Revives", 60, fmt_num, "e"), ("Respawns", "Respawns", 70, fmt_num, "e"),
            ("Death Zone", "Died at", 130, text, "w")], height=3)
        self.squad.pack(fill="x")
        self.picture = FittedImage(self)
        self.picture.pack(fill="both", expand=True, pady=(8, 0))

        self.match, self.games, self.map_mtime = None, [], None
        self.noting = None                      # (notes picture being made, its process): one at a time
        self.notes_failed = set()
        self.new_match(None)
        self.poll()

    def new_match(self, name):
        self.match = name
        self.log_mtime = self.stats_mtime = None
        self.events, self.over, self.started, self.stats = [], False, None, None
        self.picks, self.picks_read, self.reading = None, None, None   # legend select read mid-match
        if NOTES_FOLDER.is_dir():                # notes of older matches aren't looked at again
            for old in NOTES_FOLDER.iterdir():
                if old.name != name and MATCH_FOLDER.match(old.name):
                    shutil.rmtree(old, ignore_errors=True)

    def show(self, games):
        """All games (unfiltered): the workbook row gives the ranked RP once the match is in."""
        self.games = games
        self.show_match()

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
        source = Screen_Notes.sources(CAPTURES / self.match, self.events, self.stats).get(view) if self.match else None
        if not source:
            return None, "not kept for this match (screenshots go 20 min after the match is read)"
        notes = NOTES_FOLDER / self.match / f"{view}-{source.stem}.jpg"
        if notes.exists():
            return notes, f"what was read on {source.name} · click to open full size"
        if notes in self.notes_failed:
            return source, f"couldn't work out what was read on {source.name} (plain screenshot)"
        map_name = (self.stats or {}).get("map") or (self.picks or {}).get("map") or ""
        self.make_notes(notes, ["Screen_Notes.py", str(CAPTURES / self.match), view, str(notes), str(source)]
                        + ([map_name] if map_name else []))
        return source, f"working out what was read on {source.name}..."

    def guess_picture(self):
        """Ring_Predict's endgame guess for this match: from the live map's rings mid-match (LIVE_RINGS), from
        trails.json once the match is read. Remade when the rings change; the last one stays up meanwhile."""
        folder = CAPTURES / self.match
        source = folder / "trails.json" if (folder / "trails.json").exists() else None
        if not source:
            try:
                live = json.loads(LIVE_RINGS.read_text())
                if self.started and datetime.fromisoformat(live["time"]) >= self.started:
                    source = LIVE_RINGS
            except (OSError, ValueError, KeyError):
                pass
        if not source:
            return None, "no guess yet: it starts once the live map knows which map this is"
        out = NOTES_FOLDER / self.match / f"guess-{zlib.crc32(source.read_bytes()):08x}.jpg"
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

    def picked_view(self):
        """A view picked by hand: stop following the match until Follow match is clicked again."""
        self.follow.set(False)
        self.refresh_map(force=True)

    def refresh_map(self, force=False):
        if self.match:
            have = Screen_Notes.sources(CAPTURES / self.match, self.events, self.stats)
            for view, button in self.view_buttons.items():
                button.configure(state="normal" if view in ("live", "zoom", "guess", "newest") or view in have else "disabled")
            stage = STAGE_VIEWS.get(self.events[-1]["screen"]) if self.events else None
            if self.follow.get() and stage and (stage == "live" or stage in have) and stage != self.view.get():
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
        mtime = file_mtime(folder / "stats.json")
        if mtime != self.stats_mtime:
            try:
                self.stats = json.loads((folder / "stats.json").read_text())
                self.stats_mtime = mtime
            except (OSError, ValueError):
                pass
        self.read_picks(folder)
        self.show_match()

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
        if stats.get("finished"):
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
                bits = [f"squad kills {row[1]}" if row[1] != "" else "", f"landed {row[20]}" if row[20] else "",
                        "hot drop" if row[21] == "Yes" else "", f"died at {row[12]}" if row[12] else "",
                        f"lobby: {row[22]}" if row[22] else ""]
            if game and is_ranked(game) and game["rp"] is not None:
                bits.append(f"{game['rp']:+,.0f} RP")
            bits.append("in the workbook" if game else "not in the workbook yet")
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
            players = [{"Player": p.get("name"), "Legend": p.get("legend")} for p in picks.get("squad", [])]
        self.subheading.configure(text="  ·  ".join(b for b in bits if b))
        self.squad.show([dict(p, Player=(p.get("Player") or "?") + ("  (you)" if Extract_Stats.is_me(p.get("Player")) else ""))
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
        ttk.Button(book, text="Open workbook", command=lambda: open_path(WORKBOOK)).grid(
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

        self.poll_status()
        self.poll_log()
        self.poll_map()

    # --- service ---

    def poll_status(self):
        state = unit_state(SERVICE)
        colour = {"active": "#3cb44b", "activating": "#ffb000", "failed": "#e6194b"}.get(state, "grey")
        text = {"active": "running", "inactive": "stopped"}.get(state, state)
        if unit_state(RESTART_LATER) == "active":
            text += "  (restart queued for after the match)"
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
            at_bottom = self.log.yview()[1] > 0.999
            self.log.configure(state="normal")
            for tag, line in lines:
                if tag is None and re.search(r"Traceback|Error|error|failed", line):
                    tag = "problem"
                self.log.insert("end", line + "\n", tag or ())
            extra = int(self.log.index("end-1c").split(".")[0]) - LOG_LINES
            if extra > 0:
                self.log.delete("1.0", f"{extra + 1}.0")
            self.log.configure(state="disabled")
            if at_bottom:
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
        for label, width in (("Mode", 10), ("Map", 14), ("Legend", 13), ("Period", 14)):
            ttk.Label(bar, text=label + ":").pack(side="left")
            box = ttk.Combobox(bar, state="readonly", width=width, values=PERIODS if label == "Period" else [ALL])
            box.set(PERIODS[0] if label == "Period" else ALL)
            box.pack(side="left", padx=(4, 12))
            box.bind("<<ComboboxSelected>>", lambda e: self.show())
            self.filters[label] = box
        ttk.Button(bar, text="Clear filters", command=self.clear_filters).pack(side="left")
        ttk.Button(bar, text="Reload", command=self.reload).pack(side="right")
        self.status = ttk.Label(self, text="Reading the workbook...", foreground=MUTED, padding=(10, 2, 10, 4))
        self.status.pack(side="bottom", fill="x")

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.latest, self.overview, self.squad = LatestTab(tabs), OverviewTab(tabs), SquadTab(tabs)
        self.matches, self.maps = MatchesTab(tabs), MapsTab(tabs)
        tabs.add(self.latest, text="  Latest Match  ")
        tabs.add(self.overview, text="  Overview  ")
        tabs.add(self.squad, text="  Squad  ")
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
        self.status.configure(text="Reading the workbook...")

        def work():
            try:
                mtime = WORKBOOK.stat().st_mtime
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
                self.status.configure(text=f"Couldn't read the workbook ({result.__class__.__name__}), retrying...")
            else:
                self.loaded_mtime, self.games = mtime, result
                self.fill_filters()
                self.show()
        try:
            if not self.loading and WORKBOOK.stat().st_mtime != self.loaded_mtime:
                self.reload()
        except OSError:
            pass
        self.maps.poll()
        self.after(3000, self.poll)

    def fill_filters(self):
        for label, field in (("Mode", "mode"), ("Map", "map"), ("Legend", "legend")):
            counts = Counter(g[field] for g in self.games if g[field])
            self.filters[label].configure(values=[ALL] + [name for name, _ in counts.most_common()])

    def clear_filters(self):
        for label, box in self.filters.items():
            box.set(PERIODS[0] if label == "Period" else ALL)
        self.show()

    def filtered(self, ignore_map=False):
        games = self.games
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
        self.matches.show(games)
        self.maps.show(self.filtered(ignore_map=True))
        shown = f"{len(games)} of {len(self.games)} games" if len(games) != len(self.games) else f"{len(games)} games"
        when = datetime.fromtimestamp(self.loaded_mtime).strftime("%d %b %H:%M") if self.loaded_mtime else "?"
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
