"""
A desktop window for running the tracker without typing commands.

Tracker tab: is the capture running, restart / stop it, the workbook commands (Sheet_Write.py), the
latest match picture and the live capture.log. Bust a Move tab: a dancing pumpkin with kitty ears
that meows now and then; click it for the pumpkin sound.

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
MEOW_EVERY = (8, 20)                     # seconds between meows (random in this range)
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


def make_meows(folder, count=4, rate=22050):
    """Synthesise a few "mee-ow"s (pitch rises then falls, the vowel opens and closes) as wav files."""
    paths = []
    for i in range(count):
        dur = random.uniform(0.45, 0.7)
        t = np.linspace(0, dur, int(rate * dur), endpoint=False)
        u = t / dur
        base = random.uniform(480, 620)
        pitch = base * (1 + 0.55 * np.sin(np.pi * u ** 0.8)) * (1 + 0.015 * np.sin(2 * np.pi * 6 * t))
        phase = 2 * np.pi * np.cumsum(pitch) / rate
        opening = np.sin(np.pi * u) ** 0.7              # "m" → "ee-aa" → "ow"
        wave_ = sum((opening ** (k * 0.5)) / k * np.sin(k * phase) for k in range(1, 7))
        env = np.minimum(1, u / 0.08) * np.minimum(1, (1 - u) / 0.25)
        audio = wave_ * env
        audio = (audio / np.abs(audio).max() * 0.5 * 32767).astype(np.int16)
        path = Path(folder) / f"meow_{i}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(audio.tobytes())
        paths.append(path)
    return paths


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


def rotate(points, angle, ox=0.0, oy=0.0):
    c, s = math.cos(angle), math.sin(angle)
    return [(ox + (x - ox) * c - (y - oy) * s, oy + (x - ox) * s + (y - oy) * c) for x, y in points]


MOVES = ["The Pumpkin Bounce", "The Gourd Sway", "The Kitty Shimmy", "The Jack-o'-Spin"]


class DanceTab(ttk.Frame):
    """The pumpkin is drawn from polygons around its base (0, 0), so the whole thing can be squashed,
    tilted and spun each frame."""

    def __init__(self, master, notebook):
        super().__init__(master)
        self.notebook = notebook
        self.canvas = tk.Canvas(self, background="#1b1030", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.t = 0.0
        self.hop = None           # time a click-hop started
        self.bubble_until = 0.0
        self.sound_dir = tempfile.TemporaryDirectory(prefix="pumpkin_")      # deleted on exit
        self.meows = make_meows(self.sound_dir.name)
        self.next_meow = random.uniform(*MEOW_EVERY)

        c = self.canvas
        self.stars = [c.create_text(0, 0, text=random.choice("✦✧·"), fill="#f5e6a8",
                                    font=("TkDefaultFont", random.randint(8, 16))) for _ in range(40)]
        self.star_pos = [(random.random(), random.random() * 0.7) for _ in self.stars]
        self.shadow = c.create_oval(0, 0, 0, 0, fill="#0d0718", outline="")
        self.title = c.create_text(0, 30, text="", fill="#ffb347", font=("TkDefaultFont", 18, "bold"))
        self.hint = c.create_text(0, 0, text="click the pumpkin", fill="#8a7aa8", font=("TkDefaultFont", 10))

        # (item, local points, part): part decides any extra local motion (ears wiggle, pupils roll...)
        self.parts = []

        def shape(points, part=None, line=False, **style):
            create = c.create_line if line else c.create_polygon
            item = create(0, 0, 0, 0, 0, 0, tags=("pumpkin",), **style)
            self.parts.append((item, points, part))

        for side in (-1, 1):
            shape([(side * 95, -135), (side * 82, -222), (side * 35, -165)], ("ear", side),
                  fill="#e8741c", outline="#7a3500", width=3, joinstyle="round")
            shape([(side * 85, -148), (side * 79, -200), (side * 50, -163)], ("ear", side), fill="#ff9ec4")
        shape([(-6, -168), (-12, -205), (2, -215), (16, -206), (8, -168)], fill="#4a7a2a", outline="#25401a",
              width=2, smooth=True)
        for cx, rx, colour in [(-75, 62, "#d9650f"), (75, 62, "#d9650f"), (-38, 60, "#ef7b1a"),
                               (38, 60, "#ef7b1a"), (0, 56, "#ff8c25")]:
            shape(ellipse(cx, -88, rx, 88), fill=colour, outline="#9c4400", width=2)
        for side in (-1, 1):
            shape(ellipse(side * 68, -70, 18, 10), fill="#ff8f7a")
        shape([(-62, -68), (-30, -60), (0, -58), (30, -62), (64, -72), (40, -34), (0, -22), (-38, -32)],
              fill="#3a1500", outline="#3a1500", smooth=True)
        shape([(-30, -60), (-22, -60), (-26, -50)], fill="white")
        shape([(26, -62), (34, -63), (30, -52)], fill="white")
        shape(ellipse(16, -30, 15, 20), ("tongue",), fill="#ff5c8a", outline="#b0204a", width=2)
        shape([(-7, -84), (7, -84), (0, -75)], fill="#ff7aa8", outline="#7a2040", joinstyle="round")
        for side in (-1, 1):
            for dy in (-6, 2, 10):
                shape([(side * 52, -78 + dy * 0.4), (side * 115, -80 + dy * 1.8)], ("whisker", side), line=True,
                      fill="#3a1500", width=2, capstyle="round")
        self.eyes = [(-40, -112, 26), (38, -118, 20)]          # mismatched googly eyes
        for i, (ex, ey, r) in enumerate(self.eyes):
            shape(ellipse(ex, ey, r, r), fill="white", outline="#3a1500", width=2)
            shape(ellipse(ex, ey, r * 0.45, r * 0.45), ("pupil", i), fill="black")
            shape(ellipse(ex - r * 0.15, ey - r * 0.2, r * 0.13, r * 0.13), ("pupil", i), fill="white")

        self.bubble_tail = c.create_polygon(0, 0, 0, 0, 0, 0, fill="white", outline="#3a1500", width=2,
                                            state="hidden")
        self.bubble = c.create_oval(0, 0, 0, 0, fill="white", outline="#3a1500", width=2, state="hidden")
        self.bubble_text = c.create_text(0, 0, text="", fill="#3a1500", font=("TkDefaultFont", 16, "bold"),
                                         state="hidden")
        c.tag_bind("pumpkin", "<Button-1>", self.clicked)
        c.tag_bind("pumpkin", "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind("pumpkin", "<Leave>", lambda e: c.configure(cursor=""))
        self.frame()

    def visible(self):
        return self.notebook.select() == str(self)

    def clicked(self, _event):
        play(PUMPKIN_SOUND)
        self.hop = self.t

    def say(self, text, seconds=1.6):
        self.canvas.itemconfigure(self.bubble_text, text=text)
        self.bubble_until = self.t + seconds

    def frame(self):
        dt = 1 / 30
        self.t += dt
        t = self.t
        if self.visible():
            if t >= self.next_meow:
                play(random.choice(self.meows))
                self.say(random.choice(["Meow!", "Meow~", "Mrrrow!", "Meeeow"]))
                self.next_meow = t + random.uniform(*MEOW_EVERY)
            self.draw(t)
        else:
            self.next_meow = max(self.next_meow, t + 3)      # don't meow the moment you switch over
        self.after(int(dt * 1000), self.frame)

    def draw(self, t):
        c = self.canvas
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 300)
        ground = h * 0.78
        beat = 2 * math.pi * 2.0 * t                 # 120 bpm
        move = int(t // 6) % len(MOVES)

        bounce, tilt, x_shift, spin = abs(math.sin(beat / 2)) * 28, 0.0, 0.0, 0.0
        if move == 1:
            bounce *= 0.4
            tilt = math.radians(16) * math.sin(beat / 2)
            x_shift = 40 * math.sin(beat / 4)
        elif move == 2:
            bounce *= 0.3
            x_shift = 14 * math.sin(beat * 2)
            tilt = math.radians(5) * math.sin(beat * 2)
        elif move == 3:
            k = min(1, max(0, ((t % 6) / 6 - 0.3) / 0.4))      # one eased full turn mid-move
            spin = 2 * math.pi * k * k * (3 - 2 * k)
        if self.hop is not None:
            k = (t - self.hop) / 0.7
            if k >= 1:
                self.hop = None
            else:
                bounce += 140 * math.sin(math.pi * k)
                spin += 2 * math.pi * k
        squash = 1 - 0.12 * max(0, 1 - bounce / 12)
        sx, sy = 1 / squash, squash
        angle = tilt + spin
        ox, oy = w / 2 + x_shift, ground - bounce

        for item, points, part in self.parts:
            pts = points
            if part and part[0] == "ear":
                side = part[1]
                pts = rotate(pts, side * math.radians(10) * math.sin(beat + side), side * 60, -150)
            elif part and part[0] == "pupil":
                ex, ey, r = self.eyes[part[1]]
                roll = beat / 2 * (1 if part[1] == 0 else -1.3)
                cross = 0.5 * r * (1 if part[1] == 0 else -1) if move == 2 else 0
                dx, dy = cross + r * 0.4 * math.cos(roll), r * 0.4 * math.sin(roll)
                pts = [(x + dx, y + dy) for x, y in pts]
            elif part and part[0] == "tongue":
                pts = rotate(pts, math.radians(14) * math.sin(beat * 1.5), 16, -45)
            elif part and part[0] == "whisker":
                pts = rotate(pts, part[1] * math.radians(6) * math.sin(beat * 1.3), part[1] * 52, -78)
            pts = rotate([(x * sx, y * sy) for x, y in pts], angle)
            c.coords(item, *[v for x, y in pts for v in (ox + x, oy + y)])

        shadow = 110 * (1 - min(bounce, 160) / 260)
        c.coords(self.shadow, w / 2 + x_shift - shadow, ground - 10, w / 2 + x_shift + shadow, ground + 12)
        for star, (fx, fy) in zip(self.stars, self.star_pos):
            c.coords(star, fx * w, fy * h)
            c.itemconfigure(star, state="normal" if math.sin(t * 2 + fx * 40) > -0.6 else "hidden")
        c.coords(self.title, w / 2, 32)
        c.itemconfigure(self.title, text=f"Now busting: {MOVES[move]}")
        c.coords(self.hint, w / 2, h - 18)

        state = "normal" if t < self.bubble_until else "hidden"
        bx, by = ox + 120, oy - 250
        c.coords(self.bubble_tail, bx - 30, by + 15, bx - 55, by + 50, bx - 5, by + 20)
        c.coords(self.bubble, bx - 65, by - 30, bx + 65, by + 28)
        c.coords(self.bubble_text, bx, by - 1)
        for item in (self.bubble_tail, self.bubble, self.bubble_text):
            c.itemconfigure(item, state=state)


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
