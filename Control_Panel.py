"""
A desktop window for running the tracker without typing commands.

Tracker tab: is the capture running, restart / stop it, the workbook commands (Sheet_Write.py), the
latest match picture and the live capture.log. Bust a Move tab: pick a dancer (a kitty-eared pumpkin, spider,
ghost, bat, mummy or everyone at once) and a dance; they talk now and then, click one for the pumpkin sound.

Usage:
    .venv/bin/python Control_Panel.py
"""
import math
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import wave
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np
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
SERVICE = "apex-stat-capture"
RESTART_LATER = "apex-restart-later"     # the unit the README uses for ./restart_capture.sh --after-match
PUMPKIN_SOUND = Path("/home/spooky/Documents/Coding/MiniGolf/Data/Themes/halloween/Audio/Sfx/bumper/bounce_1.mp3")
VOICE_EVERY = (8, 20)                    # seconds between meows / boos / squeaks (random in this range)
LOG_LINES = 600                          # capture.log lines kept in the window
MATCH_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


# ---------------------------
# SOUND
# ---------------------------

def play(path):
    """Play a sound file in the background with whichever player is installed."""
    players = [["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["pw-play"], ["paplay"],
               ["cvlc", "--play-and-exit", "--quiet"], ["aplay", "-q"]]
    if str(path).endswith(".wav"):
        players = players[1:3] + players[4:] + players[:1]
    for cmd in players:
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd + [str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return


def voice(kind, rate=22050):
    """Synthesise one random take of a dancer's voice, as samples in -1..1."""
    if kind in ("squeak", "chitter"):
        # squeak: 2-3 high falling chirps; chitter: a quick run of tiny clicks
        parts = []
        for _ in range(random.randint(2, 3) if kind == "squeak" else random.randint(10, 16)):
            if kind == "squeak":
                u = np.linspace(0, 1, int(rate * random.uniform(0.06, 0.09)), endpoint=False)
                pitch = random.uniform(3400, 4200) * (1 - 0.35 * u)
                parts += [np.sin(2 * np.pi * np.cumsum(pitch) / rate) * np.sin(np.pi * u), np.zeros(int(rate * 0.05))]
            else:
                t = np.arange(int(rate * 0.012)) / rate
                parts += [np.sin(2 * np.pi * random.uniform(1800, 3200) * t) * np.exp(-t * 400),
                          np.zeros(int(rate * random.uniform(0.02, 0.045)))]
        return np.concatenate(parts)
    # meow / boo / groan: a voiced sound whose "mouth" opens then closes
    dur, base, harmonics, rise = {"meow": (random.uniform(0.45, 0.7), random.uniform(480, 620), 6, 0.55),
                                  "boo": (random.uniform(0.8, 1.1), random.uniform(170, 230), 4, -0.25),
                                  "groan": (random.uniform(0.9, 1.3), random.uniform(95, 125), 10, -0.2)}[kind]
    t = np.linspace(0, dur, int(rate * dur), endpoint=False)
    u = t / dur
    glide = 1 + rise * np.sin(np.pi * u ** 0.8) if rise > 0 else 1 + rise * u     # meow arcs up; boo/groan slide down
    pitch = base * glide * (1 + 0.03 * np.sin(2 * np.pi * 5.5 * t))
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    opening = np.sin(np.pi * u) ** 0.7
    audio = sum(opening ** (k * 0.5) / k ** (2 if kind == "boo" else 1) * np.sin(k * phase)
                for k in range(1, harmonics + 1))
    if kind == "groan":
        audio = audio + 0.15 * np.random.randn(len(t)) * opening
    attack = 0.08 if kind == "meow" else 0.15
    return audio * np.minimum(1, u / attack) * np.minimum(1, (1 - u) / 0.25)


def make_voices(folder, takes=3, rate=22050):
    """A few takes of every dancer's voice as wav files: {kind: [paths]}."""
    voices = {}
    for kind in {spec["voice"] for spec in CHARACTERS.values()}:
        for i in range(takes):
            audio = voice(kind, rate)
            audio = (audio / np.abs(audio).max() * 0.5 * 32767).astype(np.int16)
            path = Path(folder) / f"{kind}_{i}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(audio.tobytes())
            voices.setdefault(kind, []).append(path)
    return voices


# ---------------------------
# TRACKER TAB
# ---------------------------

def unit_state(unit):
    r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def match_folders():
    if not CAPTURES.is_dir():
        return []
    return sorted((p.name for p in CAPTURES.iterdir() if p.is_dir() and MATCH_FOLDER.match(p.name)),
                  reverse=True)


class TrackerTab(ttk.Frame):
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
        ttk.Button(book, text="Open workbook", command=lambda: self.open_path(WORKBOOK)).grid(
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
        self.map_label.bind("<Button-1>", lambda e: self.open_path(LATEST_MAP))

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
            self.open_path(picture if picture.exists() else folder)

    def open_path(self, path):
        if path.exists():
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        else:
            messagebox.showinfo("Not found", f"{path.name} doesn't exist yet.")

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
# BUST A MOVE TAB
# ---------------------------

def ellipse(cx, cy, rx, ry, n=32):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def rounded_rect(x0, y0, x1, y1, r, n=6):
    points = []
    for cx, cy, start in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        points += [(cx + r * math.cos(math.radians(start + 90 * i / n)), cy + r * math.sin(math.radians(start + 90 * i / n)))
                   for i in range(n + 1)]
    return points


def rotate(points, angle, ox=0.0, oy=0.0):
    """Rotate an (n, 2) array of points about (ox, oy)."""
    c, s = math.cos(angle), math.sin(angle)
    return (points - (ox, oy)) @ np.array([[c, s], [-s, c]]) + (ox, oy)


# --- dances ---

@dataclass
class Pose:
    """Where a dancer is this frame. lower / upper move the bottom (hips) and top (chest + head) of the
    body on their own: (dx, dy, radians)."""
    bounce: float = 0.0           # px off the ground
    x: float = 0.0                # px sideways
    tilt: float = 0.0             # radians, leaning on the feet
    spin: float = 0.0             # radians, turning about the middle
    sx: float = 1.0
    sy: float = 1.0
    lower: tuple = (0.0, 0.0, 0.0)
    upper: tuple = (0.0, 0.0, 0.0)
    limbs: float = 0.3            # how far legs / arms / wings swing (radians)
    limb_beat: float = None       # the beat they swing to (default: the music's)
    cross_eyed: bool = False


DANCES = {}


def dance(name):
    def add(fn):
        DANCES[name] = fn
        return fn
    return add


def beat(p):
    """Beat phase p seconds into the dance: abs(sin(beat / 2)) is one bounce per beat at 120 bpm."""
    return 2 * math.pi * 2 * p


def ease(k):
    k = min(1.0, max(0.0, k))
    return k * k * (3 - 2 * k)


sin, cos, D = math.sin, math.cos, math.radians


@dance("Pumpkin Bounce")
def pumpkin_bounce(p):
    return Pose(bounce=28 * abs(sin(beat(p) / 2)))


@dance("Sway")
def sway(p):
    b = beat(p)
    return Pose(bounce=11 * abs(sin(b / 2)), tilt=D(16) * sin(b / 2), x=40 * sin(b / 4))


@dance("Shimmy")
def shimmy(p):
    b = beat(p)
    return Pose(bounce=8 * abs(sin(b / 2)), x=14 * sin(2 * b), tilt=D(5) * sin(2 * b), limbs=0.2, limb_beat=3 * b,
                cross_eyed=True)


@dance("Twerking")
def twerking(p):
    b = beat(p)
    return Pose(sx=1.08, sy=0.9, tilt=D(6) * sin(b / 4), lower=(10 * sin(b / 2), -16 * abs(sin(b)), D(12) * sin(b)),
                limbs=0.5, limb_beat=2 * b)


@dance("Booby Shaking")
def booby_shaking(p):
    b = beat(p)
    return Pose(bounce=6 * abs(sin(b / 2)), upper=(13 * sin(3 * b), 3 * sin(6 * b), D(7) * sin(3 * b)),
                limbs=0.25, limb_beat=3 * b)


@dance("Floss")
def floss(p):
    b = beat(p)
    return Pose(bounce=5 * abs(sin(b)), lower=(24 * sin(b), 0, 0), upper=(-20 * sin(b), 0, 0), limbs=0.7, limb_beat=b)


@dance("Headbang")
def headbang(p):
    b = beat(p)
    nod = abs(sin(b))
    return Pose(sy=1 - 0.06 * nod, upper=(0, 18 * nod, D(10) * sin(b / 2)), limbs=0.4, limb_beat=2 * b,
                cross_eyed=True)


@dance("The Robot")
def robot(p):
    b = math.floor(beat(p) / (math.pi / 2)) * (math.pi / 2)        # jerky steps on the beat
    return Pose(tilt=D(12) * round(sin(b / 2)), x=24 * round(sin(b / 4)), upper=(0, 0, D(8) * round(cos(b / 2))),
                limbs=0.6, limb_beat=b)


@dance("Moonwalk")
def moonwalk(p):
    b = beat(p)
    glide = 4 * abs((p / 8) % 1 - 0.5) - 1                          # across and back every 8 s
    return Pose(x=150 * glide, tilt=D(-5), lower=(12 * sin(2 * b), -4 * abs(sin(2 * b)), 0), limbs=0.5,
                limb_beat=2 * b)


@dance("Jelly Wobble")
def jelly_wobble(p):
    b = beat(p)
    return Pose(bounce=6 * abs(sin(b / 2)), sx=1 + 0.14 * sin(3 * b), sy=1 - 0.14 * sin(3 * b),
                upper=(6 * sin(3 * b + 1), 0, 0))


@dance("Hula")
def hula(p):
    b = beat(p)
    return Pose(lower=(20 * cos(b / 2), 5 * sin(b), 0), upper=(-7 * cos(b / 2), 0, D(4) * cos(b / 2)), limbs=0.5,
                limb_beat=b / 2)


@dance("Belly Dance")
def belly_dance(p):
    b = beat(p)
    return Pose(x=20 * sin(b / 4), tilt=D(4) * sin(b / 4), lower=(10 * sin(3 * b), 0, D(4) * sin(3 * b)), limbs=0.6,
                limb_beat=b / 2)


@dance("Pogo")
def pogo(p):
    b = beat(p)
    return Pose(bounce=110 * abs(sin(b / 4)), tilt=D(8) * sin(b / 8), limbs=0.8, limb_beat=b)


@dance("Bunny Hop")
def bunny_hop(p):
    b = beat(p)
    return Pose(bounce=45 * abs(sin(b / 2)), x=60 * sin(b / 8), limbs=0.3, limb_beat=b)


@dance("Windmill")
def windmill(p):
    return Pose(bounce=30, spin=2 * math.pi * 0.75 * p, limbs=0.9, limb_beat=3 * beat(p))


@dance("Jack-o'-Spin")
def jack_o_spin(p):
    k = ease(((p % 3) / 3 - 0.3) / 0.4)                             # one eased turn every 3 s
    return Pose(bounce=10 * abs(sin(beat(p) / 2)), spin=2 * math.pi * k)


@dance("Disco Point")
def disco_point(p):
    b = beat(p)
    side = math.tanh(4 * sin(b / 4))                                # snaps from one side to the other
    return Pose(bounce=8 * abs(sin(b / 2)), tilt=D(18) * side, x=18 * side, upper=(10 * side, 0, D(10) * side),
                limbs=0.7, limb_beat=b / 4)


@dance("The Lean")
def the_lean(p):
    k = (p % 4) / 4
    amount = ease(k / 0.25) - ease((k - 0.75) / 0.25)               # lean over, hold, come back
    return Pose(tilt=D(30) * amount, upper=(0, 0, D(-6) * amount), limbs=0.15)


@dance("Horse Ride")
def horse_ride(p):
    b = beat(p)
    return Pose(bounce=14 * abs(sin(b)), x=26 * sin(b / 4), lower=(0, -6 * abs(sin(b)), D(6) * sin(b)), limbs=0.6,
                limb_beat=2 * b)


@dance("Funky Chicken")
def funky_chicken(p):
    b = beat(p)
    return Pose(bounce=10 * abs(sin(b / 2)), upper=(16 * sin(2 * b), 0, D(-5) * sin(2 * b)), limbs=0.9,
                limb_beat=3 * b, cross_eyed=True)


# --- dancers ---

class Figure:
    """One dancer, drawn from canvas polygons / lines around its feet (0, 0) and redrawn every frame. A part
    can have its own motion on top of the dance: ("ear", side, pivot), ("pupil", eye), ("wobble", pivot,
    degrees, speed), ("limb", side, index, pivot, "leg" | "arm" | "wing") or ("hem",)."""

    def __init__(self, canvas, name):
        spec = CHARACTERS[name]
        self.c, self.name = canvas, name
        self.height, self.hover, self.voice, self.lines = spec["height"], spec["hover"], spec["voice"], spec["lines"]
        self.tag = "dancer_" + name
        self.parts, self.eyes = [], []
        self.hop = None
        self.top = (0, 0)
        self.shadow = canvas.create_oval(0, 0, 0, 0, fill="#0d0718", outline="", tags=("shadow",))
        spec["build"](self)

    def shape(self, points, part=None, line=False, **style):
        create = self.c.create_line if line else self.c.create_polygon
        self.parts.append((create(0, 0, 0, 0, 0, 0, tags=(self.tag,), **style), np.array(points, float), part))

    def googly_eyes(self, eyes):
        for ex, ey, r in eyes:
            self.eyes.append((ex, ey, r))
            self.shape(ellipse(ex, ey, r, r), fill="white", outline="#3a1500", width=2)
            self.shape(ellipse(ex, ey, r * 0.45, r * 0.45), ("pupil", len(self.eyes) - 1), fill="black")
            self.shape(ellipse(ex - r * 0.15, ey - r * 0.2, r * 0.13, r * 0.13), ("pupil", len(self.eyes) - 1),
                       fill="white")

    def show(self, visible):
        self.c.itemconfigure(self.tag, state="normal" if visible else "hidden")
        self.c.itemconfigure(self.shadow, state="normal" if visible else "hidden")

    def move_part(self, points, part, t, b, limb_b, pose):
        kind = part[0]
        if kind == "ear":
            _, side, pivot = part
            return rotate(points, side * D(10) * sin(b + side), *pivot)
        if kind == "pupil":
            ex, ey, r = self.eyes[part[1]]
            left = part[1] % 2 == 0
            roll = b / 2 * (1 if left else -1.3)
            reach = 0.2 if pose.cross_eyed else 0.4
            cross = 0.5 * r * (1 if left else -1) if pose.cross_eyed else 0
            return points + (cross + r * reach * cos(roll), r * reach * sin(roll))
        if kind == "wobble":
            _, pivot, degrees, speed = part
            return rotate(points, D(degrees) * sin(b * speed), *pivot)
        if kind == "limb":
            _, side, i, pivot, style = part
            if style == "wing":
                angle = side * (D(25) + pose.limbs) * sin(2 * limb_b)      # bats never stop flapping
            elif style == "leg":
                angle = side * pose.limbs * sin(limb_b + i * 1.1)
            else:
                angle = pose.limbs * 1.3 * sin(limb_b)
            return rotate(points, angle, *pivot)
        if kind == "hem":
            x, y = points[:, 0], points[:, 1]
            return np.column_stack([x + 7 * np.clip((y + 50) / 50, 0, 1) * np.sin(t * 5 + x / 22), y])
        return points

    def deform(self, points, pose):
        """Move the hips (bottom of the body) and chest (top) separately, blending in between."""
        u = np.clip(-points[:, 1] / self.height, 0, 1)
        for (dx, dy, rot), weight, pivot in ((pose.lower, np.clip(1 - u / 0.5, 0, 1) ** 1.5, -0.3),
                                             (pose.upper, np.clip((u - 0.25) / 0.45, 0, 1), -0.5)):
            if dx or dy or rot:
                moved = rotate(points, rot, 0, pivot * self.height) + (dx, dy)
                points = points + weight[:, None] * (moved - points)
        return points

    def draw(self, t, b, pose, ox, ground, scale):
        bounce, spin = pose.bounce, pose.spin
        if self.hop is not None:
            k = (t - self.hop) / 0.7
            if k >= 1:
                self.hop = None
            else:
                bounce += 140 * sin(math.pi * k)
                spin += 2 * math.pi * k
        squash = 1 if self.hover else 1 - 0.12 * max(0, 1 - bounce / 12)
        sx, sy = pose.sx / squash * scale, pose.sy * squash * scale
        lift = bounce + (self.hover + 10 * sin(t * 2.5) if self.hover else 0)
        x0, y0 = ox + pose.x * scale, ground - lift * scale
        middle = -self.height * 0.45 * sy
        limb_b = b if pose.limb_beat is None else pose.limb_beat

        def place(points):
            points = self.deform(points, pose) * (sx, sy)
            return rotate(rotate(points, spin, 0, middle), pose.tilt) + (x0, y0)

        for item, points, part in self.parts:
            if part:
                points = self.move_part(points, part, t, b, limb_b, pose)
            self.c.coords(item, place(points).ravel().tolist())
        self.top = tuple(place(np.array([[0.0, -self.height]]))[0])
        r = 110 * scale * (1 - min(lift, 160) / 260)
        self.c.coords(self.shadow, x0 - r, ground - 10 * scale, x0 + r, ground + 12 * scale)


def build_pumpkin(f):
    for side in (-1, 1):
        ear = ("ear", side, (side * 60, -150))
        f.shape([(side * 95, -135), (side * 82, -222), (side * 35, -165)], ear,
                fill="#e8741c", outline="#7a3500", width=3, joinstyle="round")
        f.shape([(side * 85, -148), (side * 79, -200), (side * 50, -163)], ear, fill="#ff9ec4")
    f.shape([(-6, -168), (-12, -205), (2, -215), (16, -206), (8, -168)], fill="#4a7a2a", outline="#25401a",
            width=2, smooth=True)
    for cx, rx, colour in [(-75, 62, "#d9650f"), (75, 62, "#d9650f"), (-38, 60, "#ef7b1a"),
                           (38, 60, "#ef7b1a"), (0, 56, "#ff8c25")]:
        f.shape(ellipse(cx, -88, rx, 88), fill=colour, outline="#9c4400", width=2)
    for side in (-1, 1):
        f.shape(ellipse(side * 68, -70, 18, 10), fill="#ff8f7a")
    f.shape([(-62, -68), (-30, -60), (0, -58), (30, -62), (64, -72), (40, -34), (0, -22), (-38, -32)],
            fill="#3a1500", outline="#3a1500", smooth=True)
    f.shape([(-30, -60), (-22, -60), (-26, -50)], fill="white")
    f.shape([(26, -62), (34, -63), (30, -52)], fill="white")
    f.shape(ellipse(16, -30, 15, 20), ("wobble", (16, -45), 14, 1.5), fill="#ff5c8a", outline="#b0204a", width=2)
    f.shape([(-7, -84), (7, -84), (0, -75)], fill="#ff7aa8", outline="#7a2040", joinstyle="round")
    for side in (-1, 1):
        for dy in (-6, 2, 10):
            f.shape([(side * 52, -78 + dy * 0.4), (side * 115, -80 + dy * 1.8)], ("wobble", (side * 52, -78), side * 6, 1.3),
                    line=True, fill="#3a1500", width=2, capstyle="round")
    f.googly_eyes([(-40, -112, 26), (38, -118, 20)])          # mismatched on purpose


def build_spider(f):
    for side in (-1, 1):
        for k in range(4):
            hip = (side * 48, -98 + (k - 1.5) * 16)
            leg = ("limb", side, k, hip, "leg")
            foot = (side * (100 + k * 22), -2)
            f.shape([hip, (side * (92 + k * 14), -178 + k * 24), foot], leg, line=True, fill="#2a1d40", width=9,
                    capstyle="round", joinstyle="round", smooth=True)
            f.shape(ellipse(foot[0], foot[1] - 4, 10, 7), leg, fill="#ff7ac0", outline="#2a1d40", width=2)
    for x in (-14, 0, 14):                                    # tuft of fuzz
        f.shape([(x - 7, -176), (x * 1.3, -200 - (x == 0) * 8), (x + 7, -176)], fill="#4b3575", outline="#1d1230",
                width=2)
    f.shape(ellipse(0, -100, 80, 82), fill="#4b3575", outline="#1d1230", width=3)
    f.shape(ellipse(0, -62, 46, 34), fill="#6a4fa0")
    for side in (-1, 1):
        f.shape(ellipse(side * 54, -92, 14, 8), fill="#ff8fc0")
    for x, y in ((-46, -150), (-17, -163), (17, -163), (46, -150)):    # the extra eyes
        f.shape(ellipse(x, y, 8, 8), fill="#d8203f", outline="#1d1230", width=2)
        f.shape(ellipse(x - 2, y - 3, 2.5, 2.5), fill="white")
    f.shape([(-28, -88), (0, -80), (28, -88), (0, -64)], fill="#1d1230", outline="#1d1230", smooth=True)
    for side in (-1, 1):
        f.shape([(side * 15, -86), (side * 6, -84), (side * 11, -70)], fill="white", outline="#1d1230")
    f.googly_eyes([(-28, -118, 22), (28, -120, 19)])


def build_ghost(f):
    for side in (-1, 1):
        f.shape(ellipse(side * 88, -112, 28, 15), ("limb", side, 0, (side * 62, -115), "arm"),
                fill="#f4f1ff", outline="#9a8fc0", width=3)
    dome = [(80 * cos(a), -130 + 80 * sin(a)) for a in np.linspace(math.pi, 2 * math.pi, 14)]
    hem = [(x, 0 if i % 2 == 0 else -24) for i, x in enumerate(np.linspace(90, -90, 9))]
    f.shape(dome + [(86, -60)] + hem + [(-86, -60)], ("hem",), fill="#f4f1ff", outline="#9a8fc0", width=3,
            smooth=True)
    for side in (-1, 1):
        f.shape(ellipse(side * 50, -118, 14, 8), fill="#ffb3cf")
    for i, (ex, ey) in enumerate(((-28, -148), (28, -148))):
        f.eyes.append((ex, ey, 12))
        f.shape(ellipse(ex, ey, 13, 19), fill="#1d1a2e")
        f.shape(ellipse(ex - 3, ey - 6, 4, 5), ("pupil", i), fill="white")
    f.shape(ellipse(0, -106, 16, 20), fill="#1d1a2e")
    f.shape(ellipse(4, -94, 10, 8), ("wobble", (4, -102), 18, 1.5), fill="#ff5c8a")


def build_bat(f):
    for s in (-1, 1):
        wing = ("limb", s, 0, (s * 30, -105), "wing")
        f.shape([(s * 25, -118), (s * 80, -158), (s * 150, -140), (s * 168, -95), (s * 142, -104), (s * 122, -82),
                 (s * 98, -98), (s * 74, -76), (s * 48, -92), (s * 30, -86)], wing,
                fill="#2b2233", outline="#120c18", width=2, joinstyle="round")
        for tip in ((s * 122, -82), (s * 74, -76), (s * 150, -140)):
            f.shape([(s * 80, -158), tip], wing, line=True, fill="#4a3d58", width=2)
        ear = ("ear", s, (s * 28, -148))
        f.shape([(s * 12, -148), (s * 34, -198), (s * 44, -140)], ear, fill="#3a2f48", outline="#120c18", width=2)
        f.shape([(s * 20, -150), (s * 33, -185), (s * 38, -145)], ear, fill="#ff9ec4")
        f.shape([(s * 14, -46), (s * 18, -30)], line=True, fill="#120c18", width=4, capstyle="round")
    f.shape(ellipse(0, -100, 48, 60), fill="#3a2f48", outline="#120c18", width=2)
    f.shape(ellipse(0, -76, 28, 28), fill="#5a4a6e")
    for side in (-1, 1):
        f.shape(ellipse(side * 30, -98, 9, 5), fill="#ff8fc0")
    f.shape([(-5, -104), (5, -104), (0, -98)], fill="#ff7aa8")
    f.shape([(-16, -94), (0, -88), (16, -94), (0, -80)], fill="#120c18", outline="#120c18", smooth=True)
    for side in (-1, 1):
        f.shape([(side * 10, -92), (side * 4, -91), (side * 7, -81)], fill="white")
    f.googly_eyes([(-17, -120, 15), (18, -122, 13)])


def build_mummy(f):
    wrap, line_colour, dark = "#efe4c8", "#9c8b65", "#2a2116"
    for side in (-1, 1):
        arm = ("limb", side, 0, (side * 52, -144), "arm")
        f.shape(rounded_rect(min(side * 48, side * 128), -158, max(side * 48, side * 128), -130, 12), arm,
                fill=wrap, outline=line_colour, width=2)
        f.shape([(side * 80, -158), (side * 90, -130)], arm, line=True, fill="#cdbd94", width=3)
        f.shape(ellipse(side * 130, -144, 15, 15), arm, fill=wrap, outline=line_colour, width=2)
        f.shape(rounded_rect(side * 22 - 15, -55, side * 22 + 15, 0, 8), fill=wrap, outline=line_colour, width=2)
    f.shape(rounded_rect(-62, -225, 62, -40, 30), fill=wrap, outline=line_colour, width=3)
    for i, y in enumerate(range(-200, -50, 18)):              # bandage wraps (the eye band covers some)
        corner = max(0, -195 - y, y + 70)
        half = 62 - 30 + math.sqrt(max(0, 900 - corner ** 2)) - 4 if corner else 58
        tilt = 4 if i % 2 else -4
        f.shape([(-half, y + tilt), (half, y - tilt)], line=True, fill="#cdbd94", width=3)
    f.shape(rounded_rect(-48, -185, 48, -140, 12), fill=dark)
    for side in (-1, 1):
        f.shape(ellipse(side * 40, -118, 11, 6), fill="#ffb3a0")
    f.shape(ellipse(0, -110, 13, 9), fill=dark)
    loose = ("wobble", (48, -205), 12, 2.0)                   # a loose end flapping off the head
    f.shape([(48, -205), (78, -218), (100, -200), (120, -212)], loose, line=True, fill=line_colour, width=11,
            smooth=True, capstyle="round")
    f.shape([(48, -205), (78, -218), (100, -200), (120, -212)], loose, line=True, fill=wrap, width=7,
            smooth=True, capstyle="round")
    f.googly_eyes([(-20, -162, 15), (22, -163, 12)])


CHARACTERS = {
    "Pumpkin": dict(build=build_pumpkin, height=220, hover=0, voice="meow",
                    lines=["Meow!", "Meow~", "Mrrrow!", "Meeeow"]),
    "Spider": dict(build=build_spider, height=200, hover=0, voice="chitter",
                   lines=["Hiss!", "Skitter skitter!", "Eight legs!", "Tik tik tik"]),
    "Ghost": dict(build=build_ghost, height=210, hover=45, voice="boo",
                  lines=["Boo!", "Boooo~", "Wooooo!", "Spooky!"]),
    "Bat": dict(build=build_bat, height=180, hover=70, voice="squeak",
                lines=["Squeak!", "Eeee!", "Flap flap!", "Na na na!"]),
    "Mummy": dict(build=build_mummy, height=225, hover=0, voice="groan",
                  lines=["Mmmrrgh!", "Uuuugh...", "Need... snacks", "It's a wrap!"]),
}
EVERYONE = "Everyone!"
SHUFFLE = "Shuffle"


class DanceTab(ttk.Frame):
    def __init__(self, master, notebook):
        super().__init__(master)
        self.notebook = notebook

        bar = ttk.Frame(self, padding=(10, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Dancer:").pack(side="left")
        self.who = ttk.Combobox(bar, values=list(CHARACTERS) + [EVERYONE], state="readonly", width=11)
        self.who.set("Pumpkin")
        self.who.pack(side="left", padx=(4, 14))
        self.who.bind("<<ComboboxSelected>>", lambda e: self.cast())
        ttk.Label(bar, text="Dance:").pack(side="left")
        self.dance = ttk.Combobox(bar, values=[SHUFFLE] + list(DANCES), state="readonly", width=16)
        self.dance.set(SHUFFLE)
        self.dance.pack(side="left", padx=(4, 14))
        ttk.Label(bar, text="Speed:").pack(side="left")
        self.speed = tk.DoubleVar(value=1.0)
        ttk.Scale(bar, from_=0.5, to=2.0, variable=self.speed, length=110).pack(side="left", padx=4)
        self.voices_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Voices", variable=self.voices_on).pack(side="left", padx=(10, 0))

        self.canvas = c = tk.Canvas(self, background="#1b1030", highlightthickness=0)
        c.pack(fill="both", expand=True)
        self.t = 0.0              # real time
        self.phase = 0.0          # dance time (runs faster / slower with the speed slider)
        self.shuffled = random.choice(list(DANCES))
        self.next_shuffle = 6.0
        self.speaker = None
        self.bubble_until = 0.0
        self.sound_dir = tempfile.TemporaryDirectory(prefix="pumpkin_")      # deleted on exit
        self.voices = make_voices(self.sound_dir.name)
        self.next_voice = random.uniform(*VOICE_EVERY)

        self.stars = [c.create_text(0, 0, text=random.choice("✦✧·"), fill="#f5e6a8", tags=("star",),
                                    font=("TkDefaultFont", random.randint(8, 16))) for _ in range(40)]
        self.star_pos = [(random.random(), random.random() * 0.7) for _ in self.stars]
        self.figures = [Figure(c, name) for name in CHARACTERS]
        for f in self.figures:
            c.tag_bind(f.tag, "<Button-1>", lambda e, f=f: self.clicked(f))
            c.tag_bind(f.tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(f.tag, "<Leave>", lambda e: c.configure(cursor=""))
        c.tag_lower("shadow")
        c.tag_lower("star")
        self.title = c.create_text(0, 30, text="", fill="#ffb347", font=("TkDefaultFont", 18, "bold"))
        self.hint = c.create_text(0, 0, text="click a dancer", fill="#8a7aa8", font=("TkDefaultFont", 10))
        self.bubble_tail = c.create_polygon(0, 0, 0, 0, 0, 0, fill="white", outline="#3a1500", width=2,
                                            state="hidden")
        self.bubble = c.create_oval(0, 0, 0, 0, fill="white", outline="#3a1500", width=2, state="hidden")
        self.bubble_text = c.create_text(0, 0, text="", fill="#3a1500", font=("TkDefaultFont", 16, "bold"),
                                         state="hidden")
        self.cast()
        self.frame()

    def cast(self):
        name = self.who.get()
        self.dancers = self.figures if name == EVERYONE else [f for f in self.figures if f.name == name]
        for f in self.figures:
            f.show(f in self.dancers)
        self.bubble_until = 0.0

    def visible(self):
        return self.notebook.select() == str(self)

    def clicked(self, figure):
        play(PUMPKIN_SOUND)
        figure.hop = self.t

    def frame(self):
        dt = 1 / 30
        self.t += dt
        if self.visible():
            self.phase += dt * self.speed.get()
            if self.dance.get() == SHUFFLE and self.t >= self.next_shuffle:
                self.shuffled = random.choice([d for d in DANCES if d != self.shuffled])
                self.next_shuffle = self.t + 6
            if self.t >= self.next_voice:
                if self.voices_on.get():
                    self.speaker = random.choice(self.dancers)
                    play(random.choice(self.voices[self.speaker.voice]))
                    self.canvas.itemconfigure(self.bubble_text, text=random.choice(self.speaker.lines))
                    self.bubble_until = self.t + 1.6
                self.next_voice = self.t + random.uniform(*VOICE_EVERY)
            self.draw()
        else:
            self.next_voice = max(self.next_voice, self.t + 3)      # don't speak the moment you switch over
        self.after(int(dt * 1000), self.frame)

    def draw(self):
        c = self.canvas
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 300)
        ground = h * 0.8
        name = self.shuffled if self.dance.get() == SHUFFLE else self.dance.get()
        pose = DANCES[name](self.phase)
        n = len(self.dancers)
        scale = min(1.0, h / 600) if n == 1 else min(0.6, w / (n * 300), h / 600)
        for i, f in enumerate(self.dancers):
            f.draw(self.t, beat(self.phase), pose, w * (i + 1) / (n + 1), ground, scale)

        for star, (fx, fy) in zip(self.stars, self.star_pos):
            c.coords(star, fx * w, fy * h)
            c.itemconfigure(star, state="normal" if sin(self.t * 2 + fx * 40) > -0.6 else "hidden")
        c.coords(self.title, w / 2, 32)
        c.itemconfigure(self.title, text=f"Now busting: {name}")
        c.coords(self.hint, w / 2, h - 18)

        talking = self.speaker in self.dancers and self.t < self.bubble_until
        for item in (self.bubble_tail, self.bubble, self.bubble_text):
            c.itemconfigure(item, state="normal" if talking else "hidden")
        if talking:                       # (a hidden item has no bbox, so this comes after showing it)
            tx, ty = self.speaker.top
            bx, by = min(max(tx + 80, 90), w - 90), max(ty - 30, 70)
            c.coords(self.bubble_text, bx, by)
            x0, y0, x1, y1 = c.bbox(self.bubble_text)
            c.coords(self.bubble, x0 - 20, y0 - 12, x1 + 20, y1 + 12)
            c.coords(self.bubble_tail, bx - 25, y1 + 4, bx - 50, y1 + 34, bx - 5, y1 + 8)


# ---------------------------
# MAIN
# ---------------------------

def main():
    root = tk.Tk()
    root.title("Apex Stat Tracker")
    root.geometry("900x720")
    root.minsize(760, 560)
    ttk.Style().theme_use("clam")
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True)
    tabs.add(TrackerTab(tabs), text="  Tracker  ")
    tabs.add(DanceTab(tabs, tabs), text="  Bust a Move 🎃  ")
    root.mainloop()


if __name__ == "__main__":
    main()
