"""
The tracker's window: your stats and the capture controls in one place.

Filters along the top (mode, map, legend, time) apply to every tab:
    Overview  totals and averages, best games, and a breakdown by legend / map / POI / squad / friends...
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
import subprocess
import threading
import tkinter as tk
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

# ---------------------------
# CONFIGURATION
# ---------------------------

HERE = Path(__file__).parent
PYTHON = HERE / ".venv" / "bin" / "python"
CAPTURES = HERE / "Captures"
LOG = CAPTURES / "capture.log"
WORKBOOK = HERE / "Apex Game Data S30.ods"
LATEST_MAP = HERE / "Latest Match Map.png"
MAPS = HERE / "Maps"
SERVICE = "apex-stat-capture"
RESTART_LATER = "apex-restart-later"     # the unit the README uses for ./restart_capture.sh --after-match
LOG_LINES = 600                          # capture.log lines kept in the window
MATCH_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

# Past Games columns (letters as in the README) -> field names used here
GAME_COLUMNS = {"A": "placement", "B": "squad_kills", "C": "kills", "D": "assists", "E": "knocks", "F": "survival",
                "G": "revives", "H": "respawns", "I": "damage", "J": "legend", "K": "time", "L": "damage_share",
                "M": "death_zone", "O": "mode", "P": "map", "Q": "friends", "R": "mate_1", "S": "mate_2",
                "T": "entry", "U": "poi", "V": "contested", "AH": "rp"}
# Pictures on the Maps tab: file in Maps/<Map>/, button, what it shows
MAP_PICTURES = [("heatmap.png", "Everyone's routes",
                 "How many player-trips passed each spot (your whole squad): blue = sometimes → red = most often. "
                 "Green dots = landings."),
                ("heatmap_me.png", "My routes", "The same, for your own trails only."),
                ("rings.png", "Endgame rings",
                 "Where endgames ended up (the last ring sizes). A number = how many games ended in that circle.")]
PERIODS = ["All time", "Today", "Last 7 days", "Last 30 days", "Last 50 games", "Last 200 games"]
ALL = "All"


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
        player = dict(zip(header, row))
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
    return games


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
        self.tree.tag_configure("odd", background="#f2f3f7")
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
        self.configure(text="" if self.source else missing, image="")
        self.photo = None
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
        ttk.Label(top, text="click a heading to sort", foreground="#888").pack(side="right")
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
# MATCHES TAB
# ---------------------------

class MatchesTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
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
        self.subheading = ttk.Label(right, text="", foreground="#555")
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
        self.current = None

    def show(self, games):
        for g in games:
            g["mates_text"] = ", ".join(g["mates"])
        self.table.show(games, sort_key=self.table.sort_key or "time",
                        reverse=self.table.reverse if self.table.sort_key else True)

    def pick(self, game):
        self.current = game
        if not game:
            return
        self.heading.configure(text=f"#{fmt_num(game['placement'])} · {game['mode'] or ''} · {game['map'] or ''} · "
                                    f"{game['time']:%A %d %B %Y, %H:%M}")
        bits = [f"landed {game['poi']}" if game["poi"] else "", "hot drop" if game["contested"] == "Yes" else "",
                f"died at {game['death_zone']}" if game["death_zone"] else "",
                f"{fmt_num(game['rp'])} RP" if is_ranked(game) and game["rp"] is not None else ""]
        if not game["match"]:
            bits.append("typed in by hand: no capture for this match")
        self.subheading.configure(text="  ·  ".join(b for b in bits if b))
        self.squad.show([dict(p, Player=(p.get("Player") or "?") + ("  (you)" if p.get("Is Me") == "Yes" else ""))
                         for p in game["squad"]])
        self.picture.set(self.match_file("match_map.jpg"),
                         missing="No match picture" if game["match"] else "Not captured")
        for b, name in zip(self.buttons, ("match_map.jpg", "summary.jpg", "")):
            b.configure(state="normal" if self.match_file(name) else "disabled")

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
                               font=("TkDefaultFont", 11), selectbackground="#3d6fb6", relief="flat",
                               highlightthickness=1, highlightcolor="#c8ccd6")
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
        ttk.Label(top, text="click the picture to open it full size", foreground="#888").pack(side="right")
        self.about = ttk.Label(right, text="", foreground="#555", wraplength=700, justify="left")
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
        self.dot = tk.Canvas(top, width=18, height=18, highlightthickness=0)
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
                           background="#1e1e1e", foreground="#d4d4d4", insertbackground="white")
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
        self.status = ttk.Label(self, text="Reading the workbook...", foreground="#555", padding=(10, 2, 10, 4))
        self.status.pack(side="bottom", fill="x")

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.overview, self.matches, self.maps = OverviewTab(tabs), MatchesTab(tabs), MapsTab(tabs)
        tabs.add(self.overview, text="  Overview  ")
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
        self.overview.show(games)
        self.matches.show(games)
        self.maps.show(self.filtered(ignore_map=True))
        shown = f"{len(games)} of {len(self.games)} games" if len(games) != len(self.games) else f"{len(games)} games"
        when = datetime.fromtimestamp(self.loaded_mtime).strftime("%d %b %H:%M") if self.loaded_mtime else "?"
        self.status.configure(text=f"{shown} · workbook saved {when}")


def main():
    root = tk.Tk()
    root.title("Apex Stat Tracker")
    root.geometry("1180x820")
    root.minsize(980, 680)
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Tile.TFrame", background="white", relief="solid", borderwidth=1)
    style.configure("TileName.TLabel", background="white", foreground="#555", font=("TkDefaultFont", 9))
    style.configure("TileValue.TLabel", background="white", font=("TkDefaultFont", 17, "bold"))
    style.configure("TileNote.TLabel", background="white", foreground="#888", font=("TkDefaultFont", 8))
    style.configure("Treeview", rowheight=22)
    style.configure("Treeview.Heading", font=("TkDefaultFont", 9, "bold"))
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
