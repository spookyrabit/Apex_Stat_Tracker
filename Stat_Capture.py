"""
Lightweight background capture for Apex stats.

Watches the capture card (through OBS, or a recorded video) about once a second,
works out which Apex screen is showing, and only saves the screenshots that hold
stats: legend select, your squad, dropship, the end-of-match summary tabs, plus a
small minimap crop every so often during the match. Everything for one match lands
in its own folder under Captures/, with a match.json log of what was seen and when.

Live (OBS must be running with the WebSocket server enabled):
    python Stat_Capture.py
    python Stat_Capture.py --source "Capture Card" --password <obs websocket password>

Process an old recording instead:
    python Stat_Capture.py --video "2026-06-15 15-49-36.mp4"
"""
import argparse
import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

import Extract_Stats
import Sheet_Write
from Screen_Detection import ScreenClassifier, to_detect, crop, scale_box
from obsws_python.error import OBSSDKError
from websocket import WebSocketException

# ---------------------------
# CONFIGURATION
# ---------------------------

CAPTURES_FOLDER = Path(__file__).parent / "Captures"
OBS_SETTINGS_FILE = Path(__file__).parent / "obs_settings.json"  # optional, gitignored
# Where OBS keeps its WebSocket settings (Flatpak first, then a normal install)
OBS_WEBSOCKET_CONFIGS = [
    Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio/plugin_config/obs-websocket/config.json",
    Path.home() / ".config/obs-studio/plugin_config/obs-websocket/config.json",
]
POLL_SECONDS = 1 / 3           # how often to look at the screen (3 times a second)
EXIT_AFTER_SECONDS = 5         # how long a screen must be gone before we decide it's over
HOLD_EVERY_SECONDS = 1.0       # how often to refresh the held full frame for "last" screens
EXTRACT_SCRIPT = Path(__file__).parent / "Extract_Stats.py"
POST_GAME_SETTLE = 3           # s after the summary appears before its numbers have finished animating in
FIRST_SHOT_DELAY = {"waiting": 3}   # s on the screen before its "first" shot: the lobby is still loading in at first
MAP_SHOT_EVERY = 2             # s between frames kept while the full map stays open
PAGE_MAX_SAVES = 120            # cap on "on_change" screenshots per screen per match (summary tabs, map views)
CHANGE_THRESHOLD = 12.0        # mean pixel difference that counts as "a new tab/page"
STABLE_THRESHOLD = 4.0         # mean pixel difference that counts as "done animating"
JPEG_QUALITY = 95
# The match summary's tabs worth a shot (Extract_Stats reads these; not Death Recap / Passes, nor lobby menus
# mistaken for the summary): the selected tab has a pink-red bar under it, its name above
KEEP_SUMMARY_TABS = ("SUMMARY", "EXPERIENCE")
TAB_BAR = [450, 126, 1500, 138]    # where the selected tab's bar is (1920 wide); its label sits just above it
RESUME_WITHIN_MINUTES = 10     # restarted within this long of the last event -> same match
AFTER_SQUAD_GRACE = 15         # s; spectating another squad after yours is out counts as over this long after
PRE_GAME_SCREENS = {"waiting", "legend_select", "your_squad", "dropship"}
OBS_ERRORS = (OBSSDKError, WebSocketException, OSError)   # OBS stalled / went away: reconnect
UNKNOWN_FOLDER = CAPTURES_FOLDER / ".unknown"   # unrecognised screens between matches (new layouts to learn)
UNKNOWN_EVERY = 20             # s between them
UNKNOWN_KEEP = 40              # newest kept
STAGES = ["waiting", "legend_select", "your_squad", "dropship", "in_game", "post_game"]  # match order

# ---------------------------
# FRAME SOURCES
# ---------------------------

class ObsSource:
    """Asks OBS for screenshots of one source. Doesn't touch the capture card itself."""

    def __init__(self, host, port, password, source_name):
        import obsws_python as obs
        self.source_name = source_name
        self.client = obs.ReqClient(host=host, port=port, password=password, timeout=5)
        # Screenshots arrive as ~0.5 MB of base64 text; checking that it's valid UTF-8 in pure Python
        # took most of this program's CPU. Base64 is plain ASCII, so skip the check.
        ws = self.client.base_client.ws
        ws.frame_buffer.skip_utf8_validation = True
        ws.cont_frame.skip_utf8_validation = True
        self._full = None

    def _grab(self, width, height, fmt, quality):
        try:
            resp = self.client.get_source_screenshot(self.source_name, fmt, width, height, quality)
            data = base64.b64decode(resp.image_data.split(",", 1)[1])
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        except Exception as e:                    # any failure talking to OBS: the main loop reconnects
            raise ConnectionError(f"{e.__class__.__name__}: {e}") from e

    def next(self):
        """Small, cheap frame for detection."""
        self._full = None
        return self._grab(960, 540, "jpg", 70), datetime.now()

    def full(self):
        """Full resolution frame, fetched only when we're going to keep it."""
        if self._full is None:
            self._full = self._grab(1920, 1080, "jpg", 95)
        return self._full

class VideoSource:
    """Steps through a recording POLL_SECONDS at a time, as fast as it can decode."""

    def __init__(self, path):
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            sys.exit(f"Could not open {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.step = max(1, round(self.fps * POLL_SECONDS))
        self.index = 0
        self.frame = None
        # OBS names recordings "YYYY-MM-DD HH-MM-SS"; use that as the clock if present
        m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})", Path(path).name)
        self.start = datetime.strptime(m.group(1), "%Y-%m-%d %H-%M-%S") if m else datetime.fromtimestamp(os.path.getmtime(path))

    def next(self):
        for _ in range(self.step - 1):
            if not self.cap.grab():
                return None, None
        ok, self.frame = self.cap.read()
        if not ok:
            return None, None
        self.index += self.step
        return self.frame, self.start + timedelta(seconds=self.index / self.fps)

    def full(self):
        return self.frame

# ---------------------------
# MATCH TRACKING
# ---------------------------

class Match:
    def __init__(self, started):
        self.started = started
        self.folder = CAPTURES_FOLDER / started.strftime("%Y-%m-%d_%H-%M-%S")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.page_saves = {}          # screen -> "on_change" shots taken
        self.saved_first = set()      # screens whose "first" shot is already taken
        self.finished = False
        self.stage = -1               # furthest point of the match seen (index into STAGES)
        self.jumped_at = None         # first time in the match proper (kill feed crops count from here)

    def drop_after_match(self):
        """Delete "after the match" screenshots taken while the match was in fact still going."""
        for e in [e for e in self.events if e["event"] == "after_match"]:
            (self.folder / e["file"]).unlink(missing_ok=True)
        self.events = [e for e in self.events if e["event"] != "after_match"]
        self.page_saves["after_match"] = 0

    @classmethod
    def resume_latest(cls, when):
        """Reopen the newest match folder if it was active recently (the tool restarted mid-match)."""
        logs = sorted(CAPTURES_FOLDER.glob("*/match.json"))
        if not logs:
            return None
        with open(logs[-1]) as f:
            data = json.load(f)
        if not data["events"]:
            return None
        last_seen = datetime.fromisoformat(data["events"][-1]["time"])
        if not timedelta(0) <= when - last_seen <= timedelta(minutes=RESUME_WITHIN_MINUTES):
            return None
        match = cls(datetime.fromisoformat(data["started"]))
        match.events = data["events"]
        match.finished = data["finished"]
        match.stage = max((STAGES.index(e["screen"]) for e in match.events if e["screen"] in STAGES), default=-1)
        jumps = [e["time"] for e in match.events if e["screen"] == "in_game"]
        match.jumped_at = datetime.fromisoformat(jumps[0]) if jumps else None
        match.saved_first = {e["screen"] for e in match.events if e["event"] == "first"}
        for e in match.events:
            if e["event"] == "page":
                match.page_saves[e["screen"]] = match.page_saves.get(e["screen"], 0) + 1
        return match

    def save(self, name, image, when, screen, kind):
        path = self.folder / name
        path.parent.mkdir(exist_ok=True)
        cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        self.log(when, screen, kind, str(path.relative_to(self.folder)))
        return path

    def log(self, when, screen, event, file=None):
        entry = {"time": when.isoformat(timespec="seconds"), "screen": screen, "event": event}
        if file:
            entry["file"] = file
        self.events.append(entry)
        # Write then swap in, so anything reading it (the live map catching up) never sees half a file
        tmp = self.folder / ".match.json.tmp"
        with open(tmp, "w") as f:
            json.dump({"started": self.started.isoformat(timespec="seconds"),
                       "finished": self.finished, "events": self.events}, f, indent=2)
        os.replace(tmp, self.folder / "match.json")

class Tracker:
    """Turns a stream of per-frame screen guesses into saved screenshots."""

    def __init__(self, classifier, source, verbose=False, extract=True, live=None):
        self.classifier = classifier
        self.source = source
        self.verbose = verbose
        self.extract = extract        # read the stats out once a match's summary is done
        self.live = live              # Match_Map.LiveMap: keeps "Latest Match Map.png" current mid-match
        self.config = classifier.config
        self.match = None
        self.screen = None
        self.last_seen = None
        self.extractor = None         # running Extract_Stats.py, if any
        self.extract_pending = False
        self.last_sheet_check = None
        self.last_due = None          # last Extract_Stats.work_due() a run was started for
        self.last_map_shot = datetime.min
        self.map_check = None                 # (when, map on screen?) for the map opened while dead
        self.after_squad_at = None    # last time we watched another squad after ours was out
        self.map_locator = None
        self.held_frame = None        # latest full frame for "last" screens
        self.first_due = None         # when the screen's delayed "first" shot is due (FIRST_SHOT_DELAY)
        self.last_crop = {}           # crop name -> timer tick it was last saved on
        self.last_thumb = None        # previous poll, for stability
        self.saved_thumbs = {}        # screen -> last saved "on_change" page
        self.last_unknown = None      # last unrecognised frame kept between matches

    def policies(self, screen):
        return self.config["screens"][screen]["save"] if screen else []

    def update(self, frame, when):
        seen, scores = self.classifier.classify(frame)
        if seen in (None, "spectate") and self.map_open(frame, when):
            seen = "map"                          # the map opened while dead (no text along the bottom)
        if self.verbose:
            print(when.strftime("%H:%M:%S"), seen, {k: round(v, 2) for k, v in scores.items()})

        if seen is None:
            if self.screen and (when - self.last_seen).total_seconds() >= EXIT_AFTER_SECONDS:
                self.leave(when)
            self.between_screens(when)
            self.keep_unknown(when)
        elif seen != self.screen:
            self.leave(when)
            self.enter(seen, when)
        else:
            self.last_seen = when

        self.housekeeping(when)

        if self.screen and seen == self.screen:
            self.during(frame, when)

    def keep_unknown(self, when):
        """Between matches, keep the odd screen nothing recognised (Captures/.unknown/, newest UNKNOWN_KEEP):
        new lobby layouts (Ranked's "Squad Rank Distribution") can be turned into references from them."""
        if self.match and not self.match.finished:
            return
        if self.last_unknown and (when - self.last_unknown).total_seconds() < UNKNOWN_EVERY:
            return
        self.last_unknown = when
        UNKNOWN_FOLDER.mkdir(exist_ok=True)
        cv2.imwrite(str(UNKNOWN_FOLDER / f"{when:%Y-%m-%d_%H%M%S}.jpg"), self.source.full())
        for old in sorted(UNKNOWN_FOLDER.glob("*.jpg"))[:-UNKNOWN_KEEP]:
            old.unlink()

    def housekeeping(self, when):
        """Start Extract_Stats.py when it has something to do. Also called while waiting for OBS."""
        if self.extract and (self.last_sheet_check is None or (when - self.last_sheet_check).total_seconds() >= 60):
            self.last_sheet_check = when
            idle = self.extractor is None or self.extractor.poll() is not None
            playing = (self.match and not self.match.finished and self.match.stage == STAGES.index("in_game")
                       and self.last_seen and (when - self.last_seen).total_seconds() < 300)
            # Matches that finished while the workbook was open in Calc go in once it's closed
            if Sheet_Write.PENDING_FLAG.exists() and not Sheet_Write.lock_file(Sheet_Write.WORKBOOK).exists():
                self.extract_pending = True
            # The last match of a session (gone quiet) and screenshots due for clean-up don't wait for the
            # next match; each only once, so a match that can't be read isn't retried every minute
            elif idle and not playing:
                due = Extract_Stats.work_due()
                if due and due != self.last_due:
                    self.last_due = due
                    print(f"[{when:%H:%M:%S}] Nothing new for a while: {due}")
                    self.extract_pending = True

        if self.extract_pending and (self.extractor is None or self.extractor.poll() is not None) \
                and not self.watching_after_squad(when):
            self.run_extractor()

    def watching_after_squad(self, when):
        """Spectating another squad after yours is out: their minimap still shows the ring (often one more
        than you saw), so the match is read once that's over."""
        if not (self.match and self.match.finished):
            return False
        return self.screen in ("spectate", "map") or bool(
            self.after_squad_at and (when - self.after_squad_at).total_seconds() <= AFTER_SQUAD_GRACE)

    def enter(self, screen, when):
        self.screen = screen
        self.last_seen = when
        if screen in ("in_game", "spectate", "map") and self.match and self.match.page_saves.get("after_match"):
            self.match.drop_after_match()         # the match went on: those weren't after it after all
        self.entered_at = when
        self.late_shot_taken = False
        self.first_due = None
        if screen == "map":
            # Every time you open the map is worth a shot: the ring may have moved even though the
            # picture looks much the same (it's only a light wash over the map)
            self.saved_thumbs.pop("map", None)
        if self.match is None:
            # Started the tool mid-match: carry on with the last match if it was just running - also on a
            # pre-game screen (restarted between legend select and the dropship once split a match in two,
            # the half that was played without its legends, mode and map)
            self.match = Match.resume_latest(when)
            if self.match and not (screen in PRE_GAME_SCREENS and self.is_new_match(screen, when)):
                print(f"[{when:%H:%M:%S}] Resuming match -> {self.match.folder}")
                if self.live and not self.match.finished:
                    self.live.catch_up(self.match.folder)
            else:
                self.match = None
        if self.match is None or (screen in PRE_GAME_SCREENS and self.is_new_match(screen, when)):
            self.start_match(when)
        if screen == "post_game" and not self.match.finished:
            self.match.finished = True
        if screen in STAGES:      # the map can be opened at any point; it isn't a stage
            self.match.stage = max(self.match.stage, STAGES.index(screen))
        if screen == "in_game" and self.match.jumped_at is None:
            self.match.jumped_at = when
        self.match.log(when, screen, "enter")
        print(f"[{when:%H:%M:%S}] {screen}")
        # A screen can flicker out and back (map opened in the dropship), so keep one "first" per match
        if "first" in self.policies(screen) and screen not in self.match.saved_first:
            self.match.saved_first.add(screen)
            if screen in FIRST_SHOT_DELAY:
                self.first_due = when + timedelta(seconds=FIRST_SHOT_DELAY[screen])
            else:
                self.match.save(f"{screen}_first.jpg", self.source.full(), when, screen, "first")
        # The map is often opened for a split second: keep its very first frame straight away
        if screen == "map":
            self.last_map_shot = when
        if screen == "map" and self.match.page_saves.get("map", 0) < PAGE_MAX_SAVES:
            saves = self.match.page_saves["map"] = self.match.page_saves.get("map", 0) + 1
            full = self.source.full()
            self.match.save(f"{when:%H%M%S}_map_{saves:02d}.jpg", full, when, "map", "page")
            if self.live:
                self.live.add_map_screen(full, when)
            gray = to_detect(full, self.config)
            view = crop(gray, scale_box(__import__("Match_Map").MAP_VIEW_BOX, self.config, gray.shape[1]))
            self.saved_thumbs["map"] = cv2.resize(view, (160, 90), interpolation=cv2.INTER_AREA).astype(np.float32)

    def map_open(self, frame, when):
        """Is the full map on screen? For when it's opened while dead / spectating: then the button hints
        the map screen is recognised by aren't there, so line the middle of the screen up with the map
        picture instead. Checked every second at most (it's the slow kind of check)."""
        m = self.match
        name = self.live.last_map if self.live else None
        if not m or not name or m.stage < STAGES.index("in_game") or (m.finished and not self.watching_after_squad(when)):
            return False
        if self.map_check and (when - self.map_check[0]).total_seconds() < (1 if self.screen == "map" else 2):
            return self.map_check[1]
        import Match_Map
        if not self.map_locator or self.map_locator.map_name != name:
            from Map_Locate import MapLocator
            self.map_locator = MapLocator(name)
        view = crop(frame, scale_box(Match_Map.MAP_VIEW_BOX, self.config, frame.shape[1]))
        where = self.map_locator.locate_image(view)
        # The whole map fills that box: a lot of matches, at about the map picture's own scale or closer
        is_map = bool(where and where["inliers"] >= 40)
        self.map_check = (when, is_map)
        return is_map

    def between_screens(self, when):
        """Screens we don't recognise that still hold data: the last legend pick is revealed right after
        legend select, and a win's Champion / summary screens come after the match. Keep a few full
        screenshots of those so the stats reader can look at them."""
        m = self.match
        if not m or m.finished:
            return
        if m.stage == STAGES.index("legend_select"):
            kind, limit, every = "pick_reveal", 8, 2
        elif (m.stage == STAGES.index("in_game") and self.screen is None and m.events
              and (when - datetime.fromisoformat(m.events[-1]["time"])).total_seconds() >= 8):
            # Nothing from the match for 8 s (not just the inventory open): the match has ended
            kind, limit, every = "after_match", 20, 3
        else:
            return
        if m.page_saves.get(kind, 0) >= limit:
            return
        if self.last_crop.get(kind) and (when - self.last_crop[kind]).total_seconds() < every:
            return
        self.last_crop[kind] = when
        m.page_saves[kind] = m.page_saves.get(kind, 0) + 1
        m.save(f"{kind}/{when:%H%M%S}.jpg", self.source.full(), when, STAGES[m.stage], kind)

    def is_new_match(self, screen, when):
        """A pre-game screen starts a new match unless it belongs to the one in progress."""
        if self.match is None or self.match.finished:
            return True
        # Seeing an earlier stage again (lobby after the dropship...) or a long gap means the last
        # match never reached its summary (left early, false start): don't let it swallow this one
        went_back = STAGES.index(screen) < self.match.stage
        last_seen = datetime.fromisoformat(self.match.events[-1]["time"]) if self.match.events else self.match.started
        return went_back or when - last_seen > timedelta(minutes=RESUME_WITHIN_MINUTES)

    def during(self, frame, when):
        policies = self.policies(self.screen)
        if "last" in policies:
            if self.held_frame is None or (when - self.held_frame[1]).total_seconds() >= HOLD_EVERY_SECONDS:
                self.held_frame = (self.source.full().copy(), when)
        # Small crops (minimap, kill feed, squads left) from one full-size grab, each on its own timer.
        # Spectating after your squad is out is someone else's game (your killers): only their minimap is
        # kept, for the ring, marked "after_squad" so it's never taken for one of your squad
        if self.match.finished and self.screen in ("spectate", "map"):
            self.after_squad_at = when
        spectating_enemy = self.screen == "spectate" and self.match.finished
        shown_as = "after_squad" if spectating_enemy else self.screen
        for name, spec in self.config.get("crops", {}).items():
            if name not in policies or (spectating_enemy and name != "minimap"):
                continue
            # Timers tick on whole seconds, so crops that are due together share one full-size grab
            tick = int(when.timestamp() // spec["every"])
            if self.last_crop.get(name) == tick:
                continue
            # (counted from the first in-match frame; before that - on the dropship - always)
            if "first_seconds" in spec and self.match.jumped_at is not None and \
                    (when - self.match.jumped_at).total_seconds() > spec["first_seconds"]:
                continue
            full = self.source.full()
            # Some screens move the HUD (spectating): their own box for the crop, if they say so
            where = self.config["screens"][self.screen].get("crop_boxes", {}).get(name, spec["box"])
            box = [round(v * full.shape[1] / self.config["base_size"][0]) for v in where]
            image = crop(full, box)
            self.match.save(f"{name}/{when:%H%M%S}.jpg", image, when, shown_as, name)
            if name == "minimap" and self.live:
                self.live.add_minimap(image, when, shown_as)
            elif name == "squad" and self.live:
                self.live.add_squad_panel(image, when)
            self.last_crop[name] = tick
        if self.first_due and when >= self.first_due:
            self.save_due_first(when)
        if "on_change" in policies:
            self.save_if_new_page(frame, when)
        if self.screen == "map" and (when - self.last_map_shot).total_seconds() >= MAP_SHOT_EVERY \
                and self.match.page_saves.get("map", 0) < PAGE_MAX_SAVES:
            # The map stayed open: keep a frame every couple of seconds (it's how we follow everyone then)
            saves = self.match.page_saves["map"] = self.match.page_saves.get("map", 0) + 1
            full = self.source.full()
            self.match.save(f"{when:%H%M%S}_map_{saves:02d}.jpg", full, when, "map", "page")
            if self.live:
                self.live.add_map_screen(full, when)
            self.last_map_shot = when

    def save_due_first(self, when):
        """The delayed "first" shot (FIRST_SHOT_DELAY) of the screen we're on."""
        self.first_due = None
        self.match.save(f"{self.screen}_first.jpg", self.source.full(), when, self.screen, "first")

    def summary_tab(self, full):
        """The summary's selected tab, squashed ("DEATHRECAP", "SUMMARY", ...), or "" if its bar isn't there."""
        x0, y0, x1, y1 = scale_box(TAB_BAR, self.config, full.shape[1])
        b, g, r = (full[y0:y1, x0:x1].astype(int).max(axis=0)).T
        xs = np.where((r > 200) & (r - b > 40) & (r - g > 40))[0]
        if len(xs) < 40:
            return ""
        top = y0 - (y1 - y0) * 3
        return Extract_Stats.squash(Extract_Stats.read_line(full[top:y0, x0 + xs.min():x0 + xs.max() + 1]))

    def save_if_new_page(self, frame, when):
        gray = to_detect(frame, self.config)
        if self.screen == "map":
            # The game keeps moving either side of the map; only the map itself has to hold still
            import Match_Map
            gray = crop(gray, scale_box(Match_Map.MAP_VIEW_BOX, self.config, gray.shape[1]))
        thumb = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA).astype(np.float32)
        stable = self.last_thumb is not None and np.abs(thumb - self.last_thumb).mean() < STABLE_THRESHOLD
        self.last_thumb = thumb
        saves = self.match.page_saves.get(self.screen, 0)
        if not stable or saves >= PAGE_MAX_SAVES:
            return
        saved = self.saved_thumbs.get(self.screen)
        # The summary's placement / kill total animate in a couple of seconds after it appears: a small
        # change, so take one more shot once that's done
        late_shot = (self.screen == "post_game" and not self.late_shot_taken
                     and (when - self.entered_at).total_seconds() >= POST_GAME_SETTLE)
        if late_shot:
            self.late_shot_taken = True
        elif saved is not None and np.abs(thumb - saved).mean() < CHANGE_THRESHOLD:
            return
        self.saved_thumbs[self.screen] = thumb
        if self.screen == "post_game" and self.summary_tab(self.source.full()) not in KEEP_SUMMARY_TABS:
            return
        self.match.page_saves[self.screen] = saves + 1
        self.match.save(f"{when:%H%M%S}_{self.screen}_{saves + 1:02d}.jpg",
                        self.source.full(), when, self.screen, "page")
        if self.screen == "map" and self.live:
            self.live.add_map_screen(self.source.full(), when)

    def leave(self, when):
        if self.screen is None:
            return
        if self.first_due:                        # left before it settled: the newest frame it had
            self.save_due_first(when)
        if self.held_frame is not None:
            image, held_when = self.held_frame
            self.match.save(f"{self.screen}_last.jpg", image, held_when, self.screen, "last")
        self.match.log(when, self.screen, "leave")
        if self.screen == "post_game" and self.extract:
            self.extract_pending = True
        self.screen = None
        self.held_frame = None
        self.last_thumb = None

    def start_match(self, when):
        self.match = Match(when)
        if self.live:
            self.live.new_match()
        self.last_crop = {}
        self.saved_thumbs = {}
        print(f"[{when:%H:%M:%S}] New match -> {self.match.folder}")

    def run_extractor(self):
        """Read this match's stats in the background, at low priority so the game and OBS don't notice."""
        self.extract_pending = False
        print(f"[{datetime.now():%H:%M:%S}] Reading stats -> {EXTRACT_SCRIPT.name}")
        self.extractor = subprocess.Popen([sys.executable, str(EXTRACT_SCRIPT)], preexec_fn=lambda: os.nice(15))

    def close(self, when):
        self.leave(when or datetime.now())

# ---------------------------
# MAIN SCRIPT
# ---------------------------

def obs_config_password():
    """Read the WebSocket password straight from OBS's config, so it never has to be copied by hand."""
    for path in OBS_WEBSOCKET_CONFIGS:
        try:
            with open(path) as f:
                config = json.load(f)
        except (OSError, ValueError):
            continue
        if config.get("server_enabled"):
            return config.get("server_password", "") if config.get("auth_required") else ""
    return ""

def load_obs_settings(args):
    settings = {"host": "localhost", "port": 4455, "password": "", "source": "Capture Card"}
    if OBS_SETTINGS_FILE.exists():
        with open(OBS_SETTINGS_FILE) as f:
            settings.update(json.load(f))
    for key in settings:
        if getattr(args, key, None) is not None:
            settings[key] = getattr(args, key)
    return settings

def connect_obs(settings, idle=None):
    """Keep trying until OBS answers, so this can be left running before OBS starts. `idle(when)` is
    called while waiting (finished matches still get read)."""
    warned = False
    while True:
        try:
            # Re-read each attempt: OBS may regenerate the password when the server is re-enabled
            password = settings["password"] or obs_config_password()
            src = ObsSource(settings["host"], settings["port"], password, settings["source"])
            src.next()
            print(f"Connected to OBS, watching source '{settings['source']}'")
            return src
        except Exception as e:
            if not warned:
                print(f"Waiting for OBS WebSocket at {settings['host']}:{settings['port']} ({e.__class__.__name__}: {e})")
                warned = True
            if idle:
                idle(datetime.now())
            time.sleep(10)

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", help="process a recording instead of watching OBS")
    parser.add_argument("--source", help="OBS source name (default: Capture Card)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--password")
    parser.add_argument("--verbose", action="store_true", help="print detection scores every poll")
    args = parser.parse_args()

    # Show progress immediately when running in the background / piped to a log file
    sys.stdout.reconfigure(line_buffering=True)
    # obsws_python logs a full traceback on every failed connect; connect_obs reports it once instead
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    # Stop cleanly (saving any held "last" frame) when killed or stopped by systemd, not just on Ctrl+C
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    classifier = ScreenClassifier()
    CAPTURES_FOLDER.mkdir(exist_ok=True)

    if args.video:
        source = VideoSource(args.video)
        # Read the stats once at the end rather than racing ahead of the video
        tracker = Tracker(classifier, source, args.verbose, extract=False)
        when = None
        while True:
            frame, now = source.next()
            if frame is None:
                break
            when = now
            tracker.update(frame, when)
        tracker.close(when)
        # Old recordings / tests stay out of the workbook; add them on purpose with Sheet_Write.py
        subprocess.run([sys.executable, str(EXTRACT_SCRIPT), "--no-sheet"])
        return

    settings = load_obs_settings(args)
    import Match_Map
    cv2.setNumThreads(2)          # the live map shouldn't take over the CPU while you play
    tracker = Tracker(classifier, None, args.verbose, live=Match_Map.LiveMap())
    source = tracker.source = connect_obs(settings, idle=tracker.housekeeping)
    try:
        while True:
            tick = time.monotonic()
            try:
                frame, when = source.next()
                if frame is not None:
                    tracker.update(frame, when)
            except OBS_ERRORS as e:
                # Also while saving: the full-size grab can time out too (uncaught, that crashed the service
                # and killed the stats reader mid-read with it 3 times on 2026-10-30: two matches went unread)
                print(f"Lost OBS ({e.__class__.__name__}: {e}); reconnecting...")
                tracker.close(datetime.now())
                source = tracker.source = connect_obs(settings, idle=tracker.housekeeping)
                continue
            time.sleep(max(0.0, POLL_SECONDS - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        tracker.close(datetime.now())
        print("Stopped.")

if __name__ == "__main__":
    main()
