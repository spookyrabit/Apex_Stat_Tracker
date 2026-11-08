"""
One picture per match: the map (as high-res as the stitched minimaps allow) with every squad member's
trail, the dropship's flight, where you landed and died, and the ring wherever it was seen.

Saved as Captures/<match>/match_map.png, and copied to "Latest Match Map.png" in this folder - point an
OBS Image source at that file and it always shows the most recent game (OBS reloads it when it changes).
During a match Stat_Capture.py keeps that same file updated with your trail so far.
"""
import os
import queue
import re
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

# ---------------------------
# CONFIGURATION
# ---------------------------

HERE = Path(__file__).parent
LATEST_MAP = HERE / "Latest Match Map.png"
LATEST_ZOOM = HERE / "Latest Match Map Zoomed.png"
LIVE_RINGS = HERE / "Captures" / "live_rings.json"   # the live map's rings, for the window's endgame guess (Ring_Predict)
ZOOM_PADDING = 0.15                      # zoomed view: room around the trails (share of their extent)
ZOOM_MIN_SIZE = 0.3                      # zoomed view: never closer than this share of the map (any closer
                                         # is just blur: the map only has the minimap's own detail)
SMOOTH_WINDOW = 5                        # teammate trails: rolling median over this many sightings
MAP_VIEW_BOX = (455, 0, 1466, 1014)       # where the map is drawn on the full-map screen (1920x1080): past it
                                         # is the game and the button bar, which look like an untinted ring
OUTPUT_SIZE = 1600                       # px, longest side of the match picture
LIVE_STALL = 30                          # s; behind (5+ jobs waiting), it's still redrawn this often
LIVE_EVERY = 5                           # s between live redraws of "Latest Match Map.png"
LIVE_MAP_SHOT_EVERY = 8                  # s between map shots the live map takes while the map stays open (after 2)
LIVE_MAP_REOPEN = 4                      # s without a map shot = the map was closed (the next one is a new opening)
LIVE_ITEM_SHOTS = 2                      # map shots per opening of the map the live map looks for items on
LIVE_MAP_RETRY = 4                       # skipped shots of one opening taken after all while it hasn't shown 2 rings yet
IDENTIFY_TRIES = 60                      # minimaps to spend working out the map before giving up on this match
UNPLACED_KEEP = 60                       # minimaps not placed kept to line up backwards (about a minute)
FLIGHT_SHOT_AFTER = 90                   # s after the jump a full-map shot may still show the dropship's flight line
FLIGHT_MIN_LENGTH = 0.3                  # share of the map view the orange flight line must run along
FLIGHT_AGREE = 15                        # degrees: a flight line off the compass heading by more isn't the flight

# Squad colours: hue range (OpenCV 0-180) to find them, and the colour to draw them in (BGR)
SQUAD_COLOURS = {
    "cyan": {"hue": (80, 100), "draw": (255, 220, 40)},
    "green": {"hue": (35, 70), "draw": (70, 235, 110)},
    "orange": {"hue": (5, 25), "draw": (40, 150, 255)},
}
ARROW_AREA = (42, 90)                    # px area of a squad arrow on the minimap (teammates ~45-60, you ~60-70): bits
                                         # of same-coloured map icons (respawn beacons, Evo Harvesters, Replicators,
                                         # the ring's tint, lava) are mostly smaller, pings bigger
ARROW_BOX = 16                           # px; arrows fit in this box, map icons don't
ARROW_SHAPE = 0.4                        # most a blob's outline may differ from an arrow's (cv2.matchShapes, any
                                         # heading) - those icons aren't arrow-shaped
ARROW_PARTLY = (25, 0.15)                # smaller (partly under another arrow) still counts if it's this arrow-like
ARROW_FILE = Path(__file__).parent / "Screen_References" / "squad_arrow.png"   # an arrow's shape (yours, mid-match)
MINIMAP_SIZE = 246                       # px, the saved minimap at 1080p (arrow sizes above are at this size)
MAP_SPOT_OFF = 10                        # px (+4 px per second to the nearest minimap sighting) a full-map arrow may
                                         # sit off the way between the minimap sightings around it
TEAMMATE_SPEED = 0.02                    # map widths per second a teammate can cover (zips, redeploys)
CONFIRM_SIGHTINGS = 4                    # a teammate's trail (re)starts only after this many steady sightings
DISC_MIN_RADIUS = 50                     # px; smallest next-ring disc looked for across a whole map view
DISC_MIN_RADIUS_INSIDE = 16              # px; ...and inside the ring, where a small late one is the clearest circle
DOME_BLUER = 0.1                         # a dome's inside is at least this much bluer than the map there (tinted_inside)
DISC_ON_MAP_MAX = 0.45                   # a disc this much "lighter inside" on the map picture too is a map feature
# Survey beacon scan radius per map, as a share of the map picture's width: always the same size on a map.
# Maps not listed get no scans (teal water / map features would be guessed as scans); add one from a
# screenshot of a scan (radius on the map picture / its width).
SCAN_SIZES = {"Worlds Edge": 0.222, "Olympus": 0.222, "E-District": 0.232}   # E-District: 233-242 px, 2026-11-03
SCAN_SIZE_TOLERANCE = 0.08               # a scan's measured radius is within this share of the map's size
STRONGHOLD_DRAW = (110, 45, 255)         # BGR: the Stronghold's pink-red
EVO_DRAW = (40, 225, 235)                # BGR: the Evo Harvester's yellow
SCAN_MIN_FIT = 0.35                      # share of the scan's outline that must sit on the circle
SCAN_MIN_FILL = 0.35                     # share of the inside that must be the scan's colour
GREEN_DISC_SHARE = 0.9                   # share of a green disc's edge that's greener just inside than just outside
GREEN_DISC_IN = 1.5                      # ...how much greener than the map picture its inside is (median; real 2-7.5,
                                         # map features 0) - the ring after next, drawn as a translucent green disc
GREEN_DISC_OUT = -5                      # ...and just outside it no less green than the map picture: a ring hole with
                                         # the red tint round it reads -25 to -45 there
SCAN_NOT_RING = 0.5                      # a disc this much scan-teal inside is a scan (or its pulse), not the next ring
SCAN_SIZED, SCAN_SIZED_NOT_RING = 0.1, 0.3   # ...or this much, when it's within 10% of this map's scan size
SCAN_DRAW = (200, 210, 60)               # BGR teal the scans are drawn in
ARROW_MIN_VALUE = 100                    # brightness an arrow must reach (0-255)
ICON_RADIUS = 7                          # px on the reference: a same-coloured map icon this close = not a teammate
SQUAD_PANEL = (0, 690, 470, 1080)         # 1080p box of the squad panel, bottom left (also where it sits spectating)
LEFT_AFTER = 4                           # squad panel reads in a row without a teammate's name (yours read fine) =
                                         # they quit
SPECTATE_DECIDE = 20                     # s a spectating minimap waits for the squad panel to say whose squad it is
                                         # (other_squad_panel; read every 5 s, the death cam first): then a teammate
RING_SIZE_MATCH = 0.12                   # a ring within this share of a known ring size is that size (the same
                                         # ring measures up to ~11% apart between screenshots)
STAGE_RATIO = 0.4                        # each ring is at least this share of the one before (else a stage is missing)
RING_INSIDE_SLACK = 0.15                 # a ring may stick out of the one before by this share of its radius + 3 px
TRAIL_GAP = 8                            # s; unseen longer than this = lost: a dotted line across the gap
RESPAWN_GAP = 45                         # s; unseen this long = died and came back (Map_Locate.DOWN_GAP): no line at
                                         # all, it'd run from the death box to the beacon (2026-11-06 15:59, 214 px)
# Faster than this (share of the map width per second) is flying, not walking: skydiving from the dropship, a jump
# tower, a balloon, a respawn drop, Loba's teleport (median skydive 0.009, on foot 95% under 0.0046)
FLY_SPEED = 0.006
FLY_MIN = 0.015                          # share of the map width a fast stretch must cover to be drawn dashed
                                         # (half of them are one 6-7 px step: jitter, drawn as walking)
FONT_FILES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"]

# ---------------------------
# WHO IS WHICH COLOUR
# ---------------------------

def colour_of(hsv_pixels, min_count=6):
    """Which squad colour a patch of pixels mostly is (None if none of them)."""
    h, s, v = hsv_pixels[..., 0], hsv_pixels[..., 1], hsv_pixels[..., 2]
    bright = (s > 120) & (v > 140)
    counts = {name: int((bright & (h >= c["hue"][0]) & (h <= c["hue"][1])).sum()) for name, c in SQUAD_COLOURS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] >= min_count else None

# Where a row's colour is read on the squad panel, at 1080p: x from / to, and y from / to around the top of its name
# text - the slanted left edge of the row's frame (x ~50-77). It was x 40-115, y -30 to +50: wider than the portrait's
# edge (Mirage's orange goggles: 290 px of "orange" in a green row) and taller than a row (rows are ~65 px apart, so a
# downed teammate's red row took the green of the row under it); and over bright water / sky, the background's cyan
# outweighed a row's frame. 2026-11-06 01:29: imhungry4food49 read as cyan, then dropped as "read twice".
PANEL_FRAME = (40, 70, -10, 30)

def read_squad_panel(panel, s=1.0):
    """The squad panel (bottom left, cropped from x = 0; s = its scale from 1080p): the names on it, and
    {colour: player name} from the frame in their colour that runs down the left of each row (PANEL_FRAME)."""
    from Extract_Stats import read_text
    hsv = cv2.cvtColor(panel, cv2.COLOR_BGR2HSV)
    names, colours = [], {}
    x0, x1, dy0, dy1 = PANEL_FRAME
    for x, y, text in read_text(panel):
        name = clean_panel_name(text)
        if not name:
            continue
        names.append(name)
        colour = colour_of(hsv[max(0, y + int(dy0 * s)): y + int(dy1 * s), int(x0 * s): int(x1 * s)])
        if colour and colour not in colours:
            colours[colour] = name
    return names, colours

def clean_panel_name(text):
    """A player's name from a line of squad panel text (club tag dropped), or None if the line isn't a name
    (the level-up bar, the "4:1020822:..." ID line along the bottom)."""
    name = re.sub(r"^\[[^\]]*\]\s*", "", text).strip()
    if len(name) < 3 or re.search(r"NEXT|EVO|LVL", text.upper()) or ":" in name:
        return None
    return name

def squad_panel_of(image):
    """The squad panel cut out of a full screenshot, and its scale."""
    s = image.shape[1] / 1920
    x0, y0, x1, y1 = SQUAD_PANEL
    return image[int(y0 * s):int(y1 * s), int(x0 * s):int(x1 * s)], s

def squad_colours(dropship_image):
    """{colour: player name} from the squad panel at the bottom left (each name has a coloured frame)."""
    if dropship_image is None:
        return {}
    s = dropship_image.shape[1] / 1920
    # just the squad panel, not the rest of the HUD
    return read_squad_panel(dropship_image[int(760 * s):, : int(460 * s)], s)[1]

def on_panel(name, names):
    """Is this player's name among those read off the squad panel?"""
    from Extract_Stats import name_similarity
    return any(len(n) >= 4 and name_similarity(name, n) >= 0.7 for n in names)

def other_squad_panel(names, colours, teammates=()):
    """Spectating, whose squad panel is this (read_squad_panel's names / colours)? True = another squad's: your
    name isn't on it but 2+ others in squad-colour frames are, none of them a teammate - Apex sometimes spectates a
    random squad while yours is still alive (2026-11-05 21:22: dead, teammates up, watched insanepuff's squad until
    brought back). False = yours (your name's on it, dead or not). None = can't tell (the death cam: only the
    "Show Player Banner" hint sits in a frame)."""
    from Extract_Stats import is_me
    if any(is_me(n) for n in names):
        return False
    framed = [n for n in colours.values() if len(n) >= 3]
    if len(framed) < 2:
        return None
    return not any(on_panel(t, framed) for t in teammates)

# ---------------------------
# TEAMMATES ON THE MINIMAP
# ---------------------------

def find_arrows(minimap, colour, margin=18):
    """Centres (minimap px) of arrow-sized blobs of a squad colour, away from the minimap's edge
    (a teammate who is off the minimap is a small pointer 10-16 px inside the edge, not where they are)."""
    hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
    lo, hi = SQUAD_COLOURS[colour]["hue"]
    # (an arrow can be quite dim, e.g. green ~125 brightness)
    mask = ((hsv[..., 0] >= lo) & (hsv[..., 0] <= hi) & (hsv[..., 1] > 120) & (hsv[..., 2] > ARROW_MIN_VALUE)).astype(np.uint8)
    n, labels, stats, centres = cv2.connectedComponentsWithStats(mask)
    h, w = mask.shape
    k = w / MINIMAP_SIZE                          # sizes are at 1080p
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        cx, cy = centres[i]
        if not ARROW_PARTLY[0] * k * k <= area <= ARROW_AREA[1] * k * k or bw > ARROW_BOX * k or bh > ARROW_BOX * k:
            continue
        if area < 0.3 * bw * bh:                  # arrows are solid; icon outlines aren't
            continue
        if not margin <= cx <= w - margin or not margin <= cy <= h - margin:
            continue
        shape = cv2.matchShapes(arrow_outline(), outline((labels[y:y + bh, x:x + bw] == i).astype(np.uint8)),
                                cv2.CONTOURS_MATCH_I1, 0)
        if shape > (ARROW_SHAPE if area >= ARROW_AREA[0] * k * k else ARROW_PARTLY[1]):
            continue
        out.append((float(cx), float(cy)))
    return out

def outline(mask):
    """The outer contour of the biggest blob in a 0/1 mask."""
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    return max(cv2.findContours(padded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0], key=cv2.contourArea)

_arrow = None

def arrow_outline():
    global _arrow
    if _arrow is None:
        _arrow = outline((cv2.imread(str(ARROW_FILE), cv2.IMREAD_GRAYSCALE) > 127).astype(np.uint8))
    return _arrow

def my_colour(minimaps):
    """The colour of the arrow in the middle of the minimap (that's you), over a few minimaps."""
    votes = {}
    for minimap in minimaps:
        hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
        c = hsv.shape[0] // 2
        colour = colour_of(hsv[c - 10:c + 10, c - 10:c + 10])
        if colour:
            votes[colour] = votes.get(colour, 0) + 1
    return max(votes, key=votes.get) if votes else None

def steady(a, b, speed):
    dt = max((b["when"] - a["when"]).total_seconds(), 1)
    return dt <= TRAIL_GAP and np.hypot(b["x"] - a["x"], b["y"] - a["y"]) <= speed * dt

class TeammateTracker:
    """Follows each teammate's arrow from minimap to minimap, as positions on the reference map.
    Green / orange map icons can look like an arrow for a moment, so after a gap a trail only picks up
    again once the arrow has been seen moving steadily a few times."""

    def __init__(self, colours, margin, reference):
        self.colours = colours
        self.margin = margin                     # px cut off the minimap before the fit was made
        self.trails = {c: [] for c in colours}
        self.pending = {c: [] for c in colours}
        self.gone = set()                        # teammates who quit: their colour on the minimap is map art now
        # Respawn beacons are green, lava is orange... and they never move: they're on the map picture too
        self.reference_hsv = cv2.cvtColor(reference, cv2.COLOR_BGR2HSV)
        self.speed = TEAMMATE_SPEED * reference.shape[1]

    def on_map_icon(self, colour, x, y):
        h, w = self.reference_hsv.shape[:2]
        x, y = int(round(x)), int(round(y))
        r = ICON_RADIUS
        patch = self.reference_hsv[max(0, y - r):min(h, y + r + 1), max(0, x - r):min(w, x + r + 1)]
        return patch.size > 0 and colour_of(patch, min_count=4) == colour

    def add_point(self, colour, point):
        """A sure sighting (e.g. the teammate you're spectating, in the middle of the minimap)."""
        if colour in self.gone:
            return                               # quit: nobody to spectate
        trail = self.trails[colour]
        if trail and not steady(trail[-1], point, self.speed):
            point["gap"] = True
        trail.append(point)

    def quit(self, colour, last_seen):
        """A teammate left the match: their trail ends where they were last on the squad panel."""
        self.gone.add(colour)
        self.trails[colour] = [p for p in self.trails.get(colour, []) if p["when"] <= last_seen]
        self.pending[colour] = []

    def add(self, minimap, fit, when, me):
        for colour in self.colours:
            if colour in self.gone:
                continue
            candidates = [fit @ np.array([x - self.margin, y - self.margin, 1.0]) for x, y in find_arrows(minimap, colour)]
            candidates = [c for c in candidates if not self.on_map_icon(colour, c[0], c[1])]
            if not candidates:
                continue
            trail, pending = self.trails[colour], self.pending[colour]
            last = trail[-1] if trail and (when - trail[-1]["when"]).total_seconds() <= TRAIL_GAP else None
            anchor = (last or (pending[-1] if pending else None))
            anchor = (anchor["x"], anchor["y"]) if anchor else me
            best = min(candidates, key=lambda p: np.hypot(p[0] - anchor[0], p[1] - anchor[1]))
            point = {"when": when, "x": float(best[0]), "y": float(best[1])}
            if last:
                if steady(last, point, self.speed):
                    trail.append(point)
                continue
            # Picking the trail up again: wait for a few steady sightings in a row
            if pending and not steady(pending[-1], point, self.speed):
                pending.clear()
            pending.append(point)
            if len(pending) >= CONFIRM_SIGHTINGS:
                pending[0]["gap"] = bool(trail)
                trail.extend(pending)
                pending.clear()

# ---------------------------
# THE RING (from full-map screenshots)
# ---------------------------

def fit_circle(points, tries=300, tolerance=3.0):
    """RANSAC circle through edge points: (cx, cy, r, inliers) or None."""
    if len(points) < 30:
        return None
    rng = np.random.default_rng(0)
    best = None
    for _ in range(tries):
        a, b, c = points[rng.choice(len(points), 3, replace=False)]
        d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
        if abs(d) < 1e-6:
            continue
        ux = ((a @ a) * (b[1] - c[1]) + (b @ b) * (c[1] - a[1]) + (c @ c) * (a[1] - b[1])) / d
        uy = ((a @ a) * (c[0] - b[0]) + (b @ b) * (a[0] - c[0]) + (c @ c) * (b[0] - a[0])) / d
        r = np.hypot(a[0] - ux, a[1] - uy)
        inliers = np.abs(np.hypot(points[:, 0] - ux, points[:, 1] - uy) - r) < tolerance
        if best is None or inliers.sum() > best[3]:
            best = (ux, uy, r, int(inliers.sum()))
    return best

def ring_edges(view):
    """Edge points of the red "outside the ring" area and of the teal "next ring" disc."""
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    masks = {"ring": ((h < 8) | (h > 172)) & (s > 90) & (v > 35),
             "next ring": (h >= 82) & (h <= 98) & (s > 80) & (v > 60)}
    kernel = np.ones((9, 9), np.uint8)
    out = {}
    for name, mask in masks.items():
        mask = mask.astype(np.uint8)
        mask = cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel), cv2.MORPH_OPEN, kernel)
        if mask.mean() < 0.03:
            continue
        edge = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
        edge[:6], edge[-6:], edge[:, :6], edge[:, -6:] = 0, 0, 0, 0     # the view's own border isn't the ring
        ys, xs = np.nonzero(edge)
        out[name] = np.column_stack([xs, ys]).astype(np.float64)
    return out

def fit_known_circle(points, radius, tries=500, tolerance=2.5):
    """Centre of a circle of a known radius through edge points - works on a short arc whose centre is
    off screen (zoomed-in map). Two points + the radius give two possible centres. (cx, cy, r, inliers)."""
    if len(points) < 30:
        return None
    rng = np.random.default_rng(0)
    best = None
    for _ in range(tries):
        a, b = points[rng.choice(len(points), 2, replace=False)]
        mid, half = (a + b) / 2, np.linalg.norm(b - a) / 2
        if half < 10 or half >= radius:
            continue
        normal = np.array([-(b - a)[1], (b - a)[0]]) / (2 * half)
        for sign in (1, -1):
            c = mid + sign * normal * np.sqrt(radius ** 2 - half ** 2)
            inliers = int((np.abs(np.hypot(points[:, 0] - c[0], points[:, 1] - c[1]) - radius) < tolerance).sum())
            if best is None or inliers > best[3]:
                best = (float(c[0]), float(c[1]), float(radius), inliers)
    return best

def arc_support(edge_points, shape, circle):
    """How much of the circle's visible part really has an edge under it (0-1), and how much is visible."""
    h, w = shape
    mask = np.full((h, w), 255, np.uint8)
    for x, y in edge_points.astype(int):
        mask[y, x] = 0
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    cx, cy, r = circle[:3]
    angles = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    xs, ys = (cx + r * np.cos(angles)).astype(int), (cy + r * np.sin(angles)).astype(int)
    inside = (xs >= 8) & (xs < w - 8) & (ys >= 8) & (ys < h - 8)
    if inside.sum() == 0:
        return 0.0, 0.0
    near = distance[ys[inside], xs[inside]] <= 4
    return float(near.mean()), float(inside.mean())

def tinted_inside(view, cx, cy, r, on_map=None):
    """True if a disc is coloured inside (a blue dome shield, a survey beacon's teal scan and the pulses
    spreading out from it), not the ring's grey wash: the next ring lightens the map without changing its colour.
    `on_map`: the map picture lined up with the view - a dome is bluer than the map is there."""
    h, w = view.shape[:2]
    inside = np.zeros((h, w), np.uint8)
    cv2.circle(inside, (int(cx), int(cy)), int(r * 0.85), 255, -1)
    ring = np.zeros((h, w), np.uint8)
    cv2.circle(ring, (int(cx), int(cy)), int(r * 1.3), 255, -1)
    ring[inside > 0] = 0
    if inside.sum() == 0 or ring.sum() == 0:
        return False
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    sat_in, sat_out = hsv[..., 1][inside > 0].mean(), hsv[..., 1][ring > 0].mean()
    is_blue = (hsv[..., 0] >= 90) & (hsv[..., 0] <= 130) & (hsv[..., 1] > 80)
    blue = is_blue[inside > 0].mean()
    # A dome adds blue to the map under it; E-District's own blue-purple roofs are blue already, and the next ring's grey
    # wash takes some away: 2026-11-06 01:56-01:59, rounds 3 and 4's next rings at City Hall were 32-50% blue inside, so
    # taken for domes and dropped - the map picture there is 70-75% blue. Without the map picture: bluer than just outside
    # (round 4's: 51-56% outside; round 3's wasn't, 29% - City Hall is bluer than around it)
    if on_map is not None:
        map_hsv = cv2.cvtColor(on_map, cv2.COLOR_BGR2HSV)
        blue_there = ((map_hsv[..., 0] >= 90) & (map_hsv[..., 0] <= 130) & (map_hsv[..., 1] > 80))[inside > 0].mean()
    else:
        blue_there = is_blue[ring > 0].mean()
    # (Olympus' sky around the map is as saturated as a scan, so the saturation step misses scans there)
    return (blue > 0.35 and blue > blue_there + DOME_BLUER) or sat_in - sat_out > 40 \
        or scan_share(view, cx, cy, r) >= SCAN_NOT_RING

HOLE_SUPPORT_KNOWN = 0.45               # outline share on the circle a ring hole of a known ring size needs (else 0.6;
                                         # 2026-11-03 21:31's round 3 ring, the purple roofs breaking it up: 0.53)
# E-District's round 2 ring (155 px) on 2026-11-06 01:56's map shots: one patch fitted at 155.1, but only 36% of its
# outline on the circle (the purple roofs inside pass for the pink tint) - while the tint was 94-95% just outside, 26%
# just inside. A ring size of HOLE_RAGGED_MIN_R+ px (icons are 11-22) with HOLE_SUPPORT_RAGGED of its outline is kept
# when the tint steps that cleanly (HOLE_RAGGED_TINT: outside at least, inside at most)
HOLE_SUPPORT_RAGGED = 0.3
HOLE_RAGGED_TINT = (0.9, 0.3)
HOLE_RAGGED_MIN_R = 40
HOLE_TINT_KNOWN = 0.6                    # tint just outside a ring hole of a known ring size (else 0.75; Storm Point's round 3
                                         # ring, 2026-11-04 18:12:43: 0.71 - the tint over its orange cliffs is orange-red, past
                                         # the red hues, and POI names sit on the edge; 0% inside)
RING_RED_INSIDE = 0.15                   # most of a ring's inside that may be red tint (real rings: 0-7%)
RING_RED_OUTSIDE = 0.4                   # least of the band just outside it that must be (real rings: 49-100%)

def ring_is_tinted(view, cx, cy, r):
    """Red "outside the ring" tint just outside the circle (where it's on screen) and clear map inside it."""
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((h < 8) | (h > 172)) & (s > 90) & (v > 35)
    ys, xs = np.mgrid[:red.shape[0], :red.shape[1]]
    d = np.hypot(xs - cx, ys - cy)
    inside, outside = d <= 0.85 * r, (d >= 1.08 * r) & (d <= 1.08 * r + max(12, 0.3 * r))
    if not inside.any() or not outside.any():
        return True                               # (off screen: nothing to go on)
    return red[inside].mean() <= RING_RED_INSIDE and red[outside].mean() >= RING_RED_OUTSIDE

def scan_share(view, cx, cy, r):
    """Share of a disc's inside that's a survey beacon scan's teal."""
    inside = np.zeros(view.shape[:2], np.uint8)
    cv2.circle(inside, (int(cx), int(cy)), int(r * 0.85), 1, -1)
    return float(scan_mask(view)[inside > 0].mean()) if inside.any() else 0.0

def brighter_disc(view, min_share=0.75, min_radius=DISC_MIN_RADIUS):
    """The next ring drawn as a lighter disc (Worlds Edge, Broken Moon early on): the circle with the most
    consistent "brighter inside than outside" edge. (cx, cy, r, share) in view px, or None. A lower
    min_share finds faint discs too (used when cleaning the map, where a miss costs more than a false hit)."""
    gray = cv2.GaussianBlur(cv2.cvtColor(view, cv2.COLOR_BGR2GRAY), (0, 0), 3).astype(np.float32)
    small = cv2.resize(gray, None, fx=0.5, fy=0.5)
    circles = cv2.HoughCircles(small.astype(np.uint8), cv2.HOUGH_GRADIENT, dp=1.5, minDist=20,
                               param1=40, param2=25, minRadius=min_radius // 2, maxRadius=int(max(small.shape)))
    if circles is None:
        return None
    h, w = gray.shape
    angles = np.linspace(0, 2 * np.pi, 360, endpoint=False)
    best = None
    for cx, cy, r in circles[0][:80] * 2:
        for band in (6, 9):                       # the disc's edge is soft: look a little either side
            xi, yi = cx + (r - band) * np.cos(angles), cy + (r - band) * np.sin(angles)
            xo, yo = cx + (r + band) * np.cos(angles), cy + (r + band) * np.sin(angles)
            ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h) & (xo >= 0) & (xo < w) & (yo >= 0) & (yo < h)
            if ok.mean() < 0.3:
                continue
            step = gray[yi[ok].astype(int), xi[ok].astype(int)] - gray[yo[ok].astype(int), xo[ok].astype(int)]
            share = float((step > (6 if min_share >= 0.75 else 4)).mean())
            if share >= min_share and (best is None or share > best[3]) and not tinted_inside(view, cx, cy, r):
                best = (float(cx), float(cy), float(r), share)
    return best

def greenness(view):
    b, g, r = [c.astype(np.float32) for c in cv2.split(cv2.GaussianBlur(view, (0, 0), 3))]
    return g - (r + b) / 2

def green_disc(view, on_map, sizes=()):
    """The ring after next, drawn as a translucent green disc (Storm Point, S31: 2026-11-04 18:06:49's showed round 3
    at Ceto Station 6 min before it formed there, 148 vs 147.5 px; 18:15:47's round 4): (cx, cy, r) in view px, or None.
    Its edge is greener just inside than just outside, the map inside it greener than on the map picture, and just outside
    no less green (a ring hole has the red tint there). `sizes`: the map's ring sizes in view px - it has to be one."""
    gv = greenness(view)
    extra = gv - greenness(on_map)
    small = cv2.resize(np.clip(gv * 3 + 60, 0, 255).astype(np.uint8), None, fx=0.5, fy=0.5)
    circles = cv2.HoughCircles(small, cv2.HOUGH_GRADIENT, dp=1.5, minDist=20, param1=40, param2=20,
                               minRadius=DISC_MIN_RADIUS_INSIDE // 2, maxRadius=int(max(small.shape)))
    if circles is None:
        return None
    h, w = gv.shape
    ys, xs = np.mgrid[:h, :w]
    valid = on_map.sum(axis=2) > 0
    angles = np.linspace(0, 2 * np.pi, 360, endpoint=False)
    best = None
    for cx, cy, r in circles[0][:80] * 2:
        if sizes and not any(abs(r - s) <= max(0.12 * s, 4) for s in sizes):
            continue
        share = 0.0
        for band in (6, 9):
            xi, yi = cx + (r - band) * np.cos(angles), cy + (r - band) * np.sin(angles)
            xo, yo = cx + (r + band) * np.cos(angles), cy + (r + band) * np.sin(angles)
            ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h) & (xo >= 0) & (xo < w) & (yo >= 0) & (yo < h)
            if ok.mean() >= 0.3:
                step = gv[yi[ok].astype(int), xi[ok].astype(int)] - gv[yo[ok].astype(int), xo[ok].astype(int)]
                share = max(share, float((step > 4).mean()))
        if share < GREEN_DISC_SHARE or (best and share <= best[3]):
            continue
        d = np.hypot(xs - cx, ys - cy)
        inside, outside = (d <= 0.85 * r) & valid, (d >= 1.1 * r) & (d <= 1.1 * r + 25) & valid
        if inside.sum() < 50 or outside.sum() < 50:
            continue
        if np.median(extra[inside]) >= GREEN_DISC_IN and np.median(extra[outside]) >= GREEN_DISC_OUT:
            best = (float(cx), float(cy), float(r), share)
    return best and best[:3]

def disc_step_share(gray, circle):
    """Share of a circle's on-screen edge where it's brighter just inside than just outside, and how
    much of the circle (by length) is on screen."""
    h, w = gray.shape
    cx, cy, r = circle[:3]
    angles = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    best = (0.0, 0.0)
    for band in (6, 9):
        xi, yi = cx + (r - band) * np.cos(angles), cy + (r - band) * np.sin(angles)
        xo, yo = cx + (r + band) * np.cos(angles), cy + (r + band) * np.sin(angles)
        ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h) & (xo >= 0) & (xo < w) & (yo >= 0) & (yo < h)
        if ok.sum() < 20:
            continue
        step = gray[yi[ok].astype(int), xi[ok].astype(int)] - gray[yo[ok].astype(int), xo[ok].astype(int)]
        best = max(best, (float((step > 6).mean()), float(ok.mean())))
    return best

def partial_rings(view, scale, sizes):
    """Rings only partly on screen (zoomed in): fit each known ring size of this map to the red tint's
    edge and to the brighter disc's edge, keep fits the screenshot really backs up."""
    out = []
    if not sizes:
        return out
    gray = cv2.GaussianBlur(cv2.cvtColor(view, cv2.COLOR_BGR2GRAY), (0, 0), 3).astype(np.float32)
    edges = ring_edges(view)
    canny = cv2.Canny(gray.astype(np.uint8), 20, 50)
    canny[:6], canny[-6:], canny[:, :6], canny[:, -6:] = 0, 0, 0, 0
    ys, xs = np.nonzero(canny)
    disc_points = np.column_stack([xs, ys]).astype(np.float64)[:: max(1, len(xs) // 4000)]
    for size in sizes:
        r = size / scale                          # the ring's radius in this screenshot's pixels
        red = edges.get("ring")
        if red is not None:
            c = fit_known_circle(red[:: max(1, len(red) // 3000)], r)
            if c:
                support, visible = arc_support(red, view.shape[:2], c)
                if support >= 0.7 and visible * 2 * np.pi * r >= 150:
                    out.append(("ring", c))
        c = fit_known_circle(disc_points, r)
        if c:
            share, visible = disc_step_share(gray, c)
            if share >= 0.8 and visible * 2 * np.pi * r >= 150:
                out.append(("next ring", c))
    return out

def ring_from_hole(view, sizes=()):
    """The ring as the untinted area inside the red "outside the ring" tint: the biggest untinted patch
    whose outline is a circle, tinted just outside it and clear just inside. Works however the tint looks
    over the background (red over land, pink over Olympus' sky). (cx, cy, r, support) in view px, or None.
    `sizes`: the map's ring sizes in view px - a circle that's one of those needs less of its outline on it
    (HOLE_SUPPORT_KNOWN: E-District's purple roofs pass for the pink tint and break the outline up)."""
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = (((hue < 8) | (hue > 172)) & (sat > 90) & (val > 35))
    pink_sky = (hue >= 125) & (hue <= 175) & (sat > 15) & (val > 60)    # the tint over Olympus' sky
    red = (red | pink_sky).astype(np.uint8)
    if red.mean() < 0.15:
        return None                               # no ring tint on screen yet
    h, w = red.shape
    best = None
    # Labels / icons on the tint are closed over; red map features inside the ring (Quarantine Zone's
    # wall) can cut the hole in pieces, so also try with thin red lines filtered out
    for mask in (cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)), cv2.medianBlur(red * 255, 31) // 255):
        n, labels, stats, _ = cv2.connectedComponentsWithStats((1 - mask).astype(np.uint8))
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < 400:  # the last rings are small (radius ~20 px)
                continue
            patch = (labels == i).astype(np.uint8)
            contours, _ = cv2.findContours(patch, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            outline = max(contours, key=cv2.contourArea)[:, 0, :].astype(np.float64)
            keep = (outline[:, 0] > 6) & (outline[:, 0] < w - 7) & (outline[:, 1] > 6) & (outline[:, 1] < h - 7)
            outline = outline[keep][:: max(1, int(keep.sum()) // 1500)]
            if len(outline) < 60:
                continue
            circle = fit_circle(outline, tolerance=4)
            if not circle or circle[2] < 12 or circle[2] > 1.2 * max(h, w):
                continue                          # tiny, or a straight edge fitted as a huge circle
            support = circle[3] / len(outline)
            known = any(abs(circle[2] - s) <= 0.05 * s for s in sizes)
            usual = support >= (HOLE_SUPPORT_KNOWN if known else 0.6) and \
                tint_steps_at(red, circle, HOLE_TINT_KNOWN if known else 0.75)
            # A ring size with its outline chewed up by roofs that pass for tint, but a clean step in the tint
            ragged = known and circle[2] >= HOLE_RAGGED_MIN_R and support >= HOLE_SUPPORT_RAGGED and \
                tint_steps_at(red, circle, HOLE_RAGGED_TINT[0], HOLE_RAGGED_TINT[1])
            if ((usual or ragged) and (best is None or circle[2] > best[2])
                    and vivid_inside(view, circle) <= 0.15):
                best = (circle[0], circle[1], circle[2], support)
    return best

def vivid_inside(view, circle):
    """Share of a circle's inside that's bright and colourful (not red): a map icon on the tint (ammo,
    replicator) is; the map inside a real ring isn't."""
    h, w = view.shape[:2]
    inside = np.zeros((h, w), np.uint8)
    cv2.circle(inside, (int(circle[0]), int(circle[1])), max(1, int(circle[2] * 0.85)), 1, -1)
    if not inside.any():
        return 0.0
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    vivid = (hsv[..., 1] > 120) & (hsv[..., 2] > 120) & ~((hsv[..., 0] < 8) | (hsv[..., 0] > 172))
    return float(vivid[inside > 0].mean())

def tint_steps_at(red, circle, outside_min=0.75, inside_max=0.4):
    """Is the tint really on the outside of this circle: mostly tinted just outside its on-screen edge (`outside_min`),
    mostly clear just inside (red lines across the ring allowed for)."""
    h, w = red.shape
    cx, cy, r = circle[:3]
    angles = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    band = max(4.0, min(12.0, r * 0.1))
    xi, yi = cx + (r - band) * np.cos(angles), cy + (r - band) * np.sin(angles)
    xo, yo = cx + (r + band) * np.cos(angles), cy + (r + band) * np.sin(angles)
    ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h) & (xo >= 0) & (xo < w) & (yo >= 0) & (yo < h)
    if ok.sum() < 30:
        return False
    outside = red[yo[ok].astype(int), xo[ok].astype(int)].mean()
    inside = red[yi[ok].astype(int), xi[ok].astype(int)].mean()
    return outside >= outside_min and inside <= inside_max

def violet_mask(image):
    """The violet of Alter's ultimate (kept out of the stitched map picture)."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return ((hsv[..., 0] >= 118) & (hsv[..., 0] <= 150) & (hsv[..., 1] > 90) & (hsv[..., 2] > 15)).astype(np.uint8)

def scan_mask(image):
    """A survey beacon scan on the full map: dark teal fill, brighter teal pulses."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return ((hsv[..., 0] >= 72) & (hsv[..., 0] <= 92) & (hsv[..., 1] > 100) & (hsv[..., 2] > 30)).astype(np.uint8)

def scans_from_map_screen(locator, screenshot):
    """Survey beacon scans (teal discs) on a full-map screenshot, on the reference: [{"x", "y", "r"}].
    That colour where it's part of the map itself (it's in the map picture too) doesn't count, nor does a
    circle that isn't this map's scan size (SCAN_SIZES; maps without one get none)."""
    if locator.map_name not in SCAN_SIZES:
        return []
    size = SCAN_SIZES[locator.map_name] * locator.reference.shape[1]
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    where = locator.locate_map(view)
    if not where:
        return []
    fit, scale = where["fit"], where["scale"]
    h, w = view.shape[:2]
    on_map = cv2.warpAffine(locator.reference, cv2.invertAffineTransform(fit), (w, h))
    plain = scan_mask(view)
    mask = plain & (1 - cv2.dilate(scan_mask(on_map), np.ones((9, 9), np.uint8)))
    mask = cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    W, H = locator.reference.shape[1], locator.reference.shape[0]
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < 800:
            continue
        contours, _ = cv2.findContours((labels == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        pts = max(contours, key=cv2.contourArea)[:, 0, :].astype(np.float64)
        pts = pts[(pts[:, 0] > 5) & (pts[:, 0] < w - 6) & (pts[:, 1] > 5) & (pts[:, 1] < h - 6)]
        pts = pts[:: max(1, len(pts) // 1500)]
        circle = fit_circle(pts, tolerance=4) if len(pts) > 40 else None
        if not circle or circle[3] < SCAN_MIN_FIT * len(pts):
            continue
        cx, cy = fit @ np.array([circle[0], circle[1], 1.0])
        r = circle[2] * scale
        if abs(r - size) > SCAN_SIZE_TOLERANCE * size or not (0 <= cx <= W and 0 <= cy <= H):
            continue
        inside = np.zeros((h, w), np.uint8)
        cv2.circle(inside, (int(circle[0]), int(circle[1])), int(circle[2] * 0.9), 1, -1)
        if inside.sum() == 0 or plain[inside > 0].mean() < SCAN_MIN_FILL:
            continue                              # a real scan is that colour all the way through
        out.append({"x": float(cx), "y": float(cy), "r": float(r), "fit": circle[3] / len(pts)})
    # The scan's pulses (rings spreading out from it) fit circles of their own: of circles overlapping
    # by more than half the smaller one's radius, keep the best fit
    kept = []
    for scan in sorted(out, key=lambda sc: -sc["fit"]):
        if not any(np.hypot(scan["x"] - k["x"], scan["y"] - k["y"]) < k["r"] + scan["r"] / 2 for k in kept):
            kept.append(scan)
    return [{k: v for k, v in sc.items() if k != "fit"} for sc in kept]

MAP_ARROW_AREA = (60, 450)               # px area of a squad arrow on the full map (bigger than on the minimap)
MAP_ARROW_BOX = 40

def squad_on_map_screen(locator, screenshot):
    """Squad arrows on a full-map screenshot: {colour: [(x, y), ...]} on the reference. While the map is
    open there's no minimap, so this is what keeps everyone's trail going."""
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    where = locator.locate_map(view)
    if not where:
        return {}
    fit = where["fit"]
    h, w = view.shape[:2]
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    ref_hsv = cv2.cvtColor(cv2.warpAffine(locator.reference, cv2.invertAffineTransform(fit), (w, h)), cv2.COLOR_BGR2HSV)
    out = {}
    for colour, spec in SQUAD_COLOURS.items():
        lo, hi = spec["hue"]
        mask = ((hsv[..., 0] >= lo) & (hsv[..., 0] <= hi) & (hsv[..., 1] > 120) & (hsv[..., 2] > ARROW_MIN_VALUE)).astype(np.uint8)
        n, _, stats, centres = cv2.connectedComponentsWithStats(mask)
        found = []
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            if not MAP_ARROW_AREA[0] <= area <= MAP_ARROW_AREA[1] or max(bw, bh) > MAP_ARROW_BOX:
                continue
            cx, cy = centres[i]
            patch = ref_hsv[max(0, by):by + bh, max(0, bx):bx + bw]
            if patch.size and colour_of(patch, min_count=max(4, area // 4)) == colour:
                continue                          # that colour is on the map picture there too: lava, an icon
            found.append(tuple(float(v) for v in fit @ np.array([cx, cy, 1.0])))
        if found:
            out[colour] = found
    return out

def fill_from_map_screens(trail, sightings, time_key, max_jump=60, max_gap=30):
    """Add map-screen positions to a trail: at each map shot, the arrow of that colour nearest to where the
    player last was (if it's a believable distance away). Between two minimap sightings it must also be on the
    way from one to the other: the nearest orange to you on the full map can be lava. `sightings`:
    [(datetime, [(x, y), ...])]."""
    from datetime import datetime
    when = (lambda p: p[time_key] if isinstance(p[time_key], datetime) else datetime.fromisoformat(p[time_key]))
    added = []
    for t, spots in sightings:
        before = [p for p in trail + added if when(p) <= t]
        if not before or not spots or any(when(p) == t for p in trail):
            continue                              # (a minimap at that very moment already placed them)
        last = max(before, key=when)
        if (t - when(last)).total_seconds() > max_gap:
            continue
        after = [p for p in trail if when(p) > t and (when(p) - t).total_seconds() <= max_gap]
        if after:
            nxt = min(after, key=when)
            share = (t - when(last)).total_seconds() / (when(nxt) - when(last)).total_seconds()
            ex, ey = last["x"] + share * (nxt["x"] - last["x"]), last["y"] + share * (nxt["y"] - last["y"])
            x, y = min(spots, key=lambda q: np.hypot(q[0] - ex, q[1] - ey))
            near = min((t - when(last)).total_seconds(), (when(nxt) - t).total_seconds())
            if np.hypot(x - ex, y - ey) > MAP_SPOT_OFF + 4 * near:
                continue
        else:
            x, y = min(spots, key=lambda q: np.hypot(q[0] - last["x"], q[1] - last["y"]))
        if np.hypot(x - last["x"], y - last["y"]) <= max_jump:
            point = {"x": x, "y": y, "from_map": True,
                     time_key: t if isinstance(last[time_key], datetime) else t.isoformat(timespec="seconds")}
            if "screen" in last:
                point["screen"] = "in_game"
            added.append(point)
    if added:
        trail.extend(added)
        trail.sort(key=when)
    return len(added)

def distinct_scans(scans):
    """The same scan seen on several screenshots counts once (first time seen, averaged position)."""
    groups = []
    for scan in sorted(scans, key=lambda s: s.get("time", "")):
        for g in groups:
            if np.hypot(g[0]["x"] - scan["x"], g[0]["y"] - scan["y"]) <= 0.3 * scan["r"]:
                g.append(scan)
                break
        else:
            groups.append([scan])
    return [{"x": float(np.median([s["x"] for s in g])), "y": float(np.median([s["y"] for s in g])),
             "r": float(np.median([s["r"] for s in g])), "time": g[0].get("time", ""), "seen": len(g)} for g in groups]

def disc_inside(disc, ring):
    return disc[2] < ring[2] and np.hypot(disc[0] - ring[0], disc[1] - ring[1]) + disc[2] <= ring[2] * 1.05

def next_ring_inside(view, ring, on_map, sizes=()):
    """The next ring's lighter disc looked for just inside the ring (cx, cy, r in view px): across the whole
    view a small late disc is lost among the map's other circles. `on_map`: the map picture lined up with
    the view. Circles are looked for in what's drawn over the map (view minus map picture), each nudged a few
    px to where the most of its edge is brighter inside; one that's a lighter circle on the map picture too
    is a map feature. The biggest clear disc wins. (cx, cy, r, share) in view px, or None.
    `sizes`: this map's ring sizes in view px - the next size down from the ring is also looked for straight away
    (known_size_discs), as Hough's centres can be too far off to nudge: 2026-11-06 01:59, round 4's next ring
    (48 px, edge 93-98% lighter inside) with squad arrows and icons over a third of it, Hough's nearest guesses
    30 px out - never found, live or final, so no ring past round 3 was drawn."""
    h, w = view.shape[:2]
    pad = ring[2] * 1.1
    x0, y0 = max(0, int(ring[0] - pad)), max(0, int(ring[1] - pad))
    x1, y1 = min(w, int(ring[0] + pad)), min(h, int(ring[1] + pad))
    if x1 - x0 < 40 or y1 - y0 < 40:
        return None
    blur = lambda im: cv2.GaussianBlur(cv2.cvtColor(im[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (0, 0), 3).astype(np.float32)
    gray, plain = blur(view), blur(on_map)
    over = cv2.resize(np.clip(gray - plain + 40, 0, 255), None, fx=0.5, fy=0.5)
    circles = cv2.HoughCircles(over.astype(np.uint8), cv2.HOUGH_GRADIENT, dp=1.5, minDist=20, param1=40, param2=20,
                               minRadius=DISC_MIN_RADIUS_INSIDE // 2, maxRadius=int(max(over.shape)))
    guesses = list(circles[0][:30] * 2) if circles is not None else []
    guesses += known_size_discs(gray, (ring[0] - x0, ring[1] - y0, ring[2]), sizes)        # (in the crop already)
    best = None
    for cx, cy, r in guesses:
        if disc_step_share(gray, (cx, cy, r))[0] < 0.3:
            continue                              # not even roughly a lighter disc
        local = None
        for step, reach in ((4, 8), (1, 2)):     # coarse, then fine around the best so far
            sx, sy, sr = (local[:3] if local else (cx, cy, r))
            for dx in range(-reach, reach + 1, step):
                for dy in range(-reach, reach + 1, step):
                    for dr in range(-reach, reach + 1, step):
                        c = (sx + dx, sy + dy, sr + dr)
                        if c[2] < DISC_MIN_RADIUS_INSIDE:
                            continue
                        share, visible = disc_step_share(gray, c)
                        if visible >= 0.8 and (local is None or share > local[3]):
                            local = (c[0], c[1], c[2], share)
        if not local or local[3] < 0.8 or disc_step_share(plain, local)[0] >= DISC_ON_MAP_MAX:
            continue                              # not a clear disc, or one that's part of the map picture
        # Icons make small lighter circles too: the next ring is the biggest lighter disc inside the ring
        disc = (local[0] + x0, local[1] + y0, local[2], local[3])
        if (best is None or disc[2] > best[2]) and disc_inside(disc, ring) and not tinted_inside(view, *disc[:3], on_map):
            best = disc
    return best

KNOWN_DISC_STEPS = 12                    # known_size_discs: centres tried across the room inside the ring, each way

def known_size_discs(gray, ring, sizes):
    """The next ring is the next ring size down: the biggest size smaller than `ring` (cx, cy, r; view px) - also when
    the ring is closing to it - and the circle of that size inside it whose edge is most often lighter just inside
    (disc_step_share), from a grid of centres (KNOWN_DISC_STEPS each way across the room it has). [(cx, cy, r)] (or [])
    for next_ring_inside to nudge and check like Hough's. (Every smaller size: inside the 155 px ring a 42 px lighter
    patch passed, 2026-11-06 01:56:09.)"""
    out = []
    smaller = [size for size in sizes if DISC_MIN_RADIUS_INSIDE <= size < ring[2] * 0.9]
    for size in sorted(smaller)[-1:]:
        room = ring[2] - size
        step = max(2.0, 2 * room / KNOWN_DISC_STEPS)
        best = None
        for dx in np.arange(-room, room + 0.1, step):
            for dy in np.arange(-room, room + 0.1, step):
                if dx * dx + dy * dy > room * room:
                    continue
                c = (ring[0] + dx, ring[1] + dy, size)
                share, visible = disc_step_share(gray, c)
                if visible >= 0.8 and (best is None or share > best[1]):
                    best = (c, share)
        if best:
            out.append(best[0])
    return out

def rings_from_map_screen(locator, screenshot, prior=None):
    """Ring circles (on the reference) seen on one full-map screenshot. `prior`: Map_Locate.ring_prior() of the
    rings found so far - every ring is inside the one before, so nothing bigger or outside it is looked for."""
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    where = locator.locate_map(view)
    if not where:
        return []
    fit, scale = where["fit"], where["scale"]
    out = []
    hole = ring_from_hole(view, [s / scale for s in locator.ring_sizes])
    on_map = cv2.warpAffine(locator.reference, cv2.invertAffineTransform(fit), view.shape[1::-1])
    plain = cv2.GaussianBlur(cv2.cvtColor(on_map, cv2.COLOR_BGR2GRAY), (0, 0), 3).astype(np.float32)
    disc = brighter_disc(view)
    if disc:
        if disc_step_share(plain, disc)[0] >= DISC_ON_MAP_MAX:
            disc = None                            # a lighter circle that's part of the map itself
    faint = False
    if hole:
        # The next ring sits inside the ring: look there first. A disc found across the whole view that's
        # inside the ring but not found there is doubtful too: both have to be seen twice (consistent_rings)
        inside = next_ring_inside(view, hole, on_map, [s / scale for s in locator.ring_sizes])
        if inside:
            disc, faint = inside, True
        elif disc and disc_inside(disc, hole):
            faint = True
    if disc:
        cx, cy = fit @ np.array([disc[0], disc[1], 1.0])
        out.append({"kind": "next ring", "x": float(cx), "y": float(cy), "r": float(disc[2] * scale)})
        if faint:
            out[-1]["faint"] = True                # small and faint: counts once seen twice (consistent_rings)
    found_kinds = {"next ring"} if disc else set()
    if hole:
        cx, cy = fit @ np.array([hole[0], hole[1], 1.0])
        out.append({"kind": "ring", "x": float(cx), "y": float(cy), "r": float(hole[2] * scale)})
        found_kinds.add("ring")
    for kind, points in ring_edges(view).items():
        if kind in found_kinds:
            continue
        circle = fit_circle(points[:: max(1, len(points) // 3000)])
        if not circle or circle[3] < 60 or circle[2] < 25:
            continue
        # A real ring edge runs along most of whatever part of the circle is on screen
        support, visible = arc_support(points, view.shape[:2], circle)
        if support < 0.6 or visible < 0.25:
            continue
        cx, cy = fit @ np.array([circle[0], circle[1], 1.0])
        out.append({"kind": kind, "x": float(cx), "y": float(cy), "r": float(circle[2] * scale)})
        found_kinds.add(kind)
    # Zoomed in, a ring may only show as an arc: try this map's known ring sizes on what's left
    known_ring = (prior or (None, None))[0]
    sizes = [s for s in locator.ring_sizes if not known_ring or s <= known_ring[2] * 1.05]
    for kind, circle in partial_rings(view, scale, sizes):
        if kind in found_kinds:
            continue
        cx, cy = fit @ np.array([circle[0], circle[1], 1.0])
        out.append({"kind": kind, "x": float(cx), "y": float(cy), "r": float(circle[2] * scale), "partial": True})
        found_kinds.add(kind)
    # The ring after next, drawn green (as well as the grey next ring): another next ring
    green = green_disc(view, on_map, [s / scale for s in locator.ring_sizes])
    if green:
        cx, cy = fit @ np.array([green[0], green[1], 1.0])
        same = lambda r: abs(r["r"] - green[2] * scale) <= 0.1 * r["r"] + 3 and np.hypot(r["x"] - cx, r["y"] - cy) <= 0.1 * r["r"] + 3
        if not any(same(r) for r in out):              # (not the ring / next ring already found)
            out.append({"kind": "next ring", "x": float(cx), "y": float(cy), "r": float(green[2] * scale), "green": True})
    # However it was found, a next ring that's mostly scan-teal inside is a survey beacon's scan or its pulse; one the
    # size of this map's scan needs less (E-District's purple roofs read bluer than teal under it: 45%, 2026-11-03 18:27)
    back = cv2.invertAffineTransform(fit)
    scan_r = SCAN_SIZES.get(locator.map_name, 0) * locator.reference.shape[1]
    def scan_like(r):
        share = scan_share(view, *(back @ np.array([r["x"], r["y"], 1.0])), r["r"] / scale)
        return share >= SCAN_NOT_RING or (abs(r["r"] - scan_r) <= SCAN_SIZED * scan_r and share >= SCAN_SIZED_NOT_RING)
    out = [r for r in out if r["kind"] != "next ring" or not scan_like(r)]
    # ...and one that's a lighter circle on the map picture too is part of the map (Thermal Station's round
    # building fitted with the 60 m ring size)
    out = [r for r in out if r["kind"] != "next ring" or
           disc_step_share(plain, (*(back @ np.array([r["x"], r["y"], 1.0])), r["r"] / scale))[0] < DISC_ON_MAP_MAX]
    # ...and a ring has the red tint all round its outside and clear map inside (a red marker circle or a bit
    # of lava fitted with a small ring size doesn't)
    out = [r for r in out if r["kind"] != "ring" or ring_is_tinted(view, *(back @ np.array([r["x"], r["y"], 1.0])), r["r"] / scale)]
    if known_ring:
        from Map_Locate import inside_ring
        # A whole ring (not an arc) the known ring's size is that ring, seen sharper: it takes the known one's place.
        # The known one can be a minimap fit gone wrong, and inside it nothing real is left to find (2026-11-07 22:14,
        # E-District: round 2's closing edge taken as its ring, 45 px off; the map shots' round 2 ring and round 3 next
        # ring stuck out of it and were thrown away, and every ring after but the last with them)
        whole = next((r for r in out if r["kind"] == "ring" and not r.get("partial")
                      and abs(r["r"] - known_ring[2]) <= 0.15 * known_ring[2] + 3), None)
        if whole:
            known_ring = (whole["x"], whole["y"], whole["r"])
        out = [r for r in out if inside_ring((r["x"], r["y"], r["r"]), known_ring)]
    ring = next((r for r in out if r["kind"] == "ring"), None)
    if ring:
        out = [r for r in out if r["kind"] == "ring" or
               (r["r"] < ring["r"] and np.hypot(r["x"] - ring["x"], r["y"] - ring["y"]) + r["r"] <= ring["r"] * 1.05)]
    return out

# ---------------------------
# DRAWING
# ---------------------------

def dashed_line(image, a, b, colour, width, dash=10, phase=0.0):
    """`phase`: how far into the dash pattern the line starts. Returns where it ends, to pass on to the next
    piece, so a path made of many short pieces stays evenly dashed."""
    a, b = np.array(a, float), np.array(b, float)
    length = np.linalg.norm(b - a)
    for start in np.arange(-phase, length, 2 * dash):
        if start + dash <= 0:
            continue
        p = a + (b - a) * max(start, 0) / max(length, 1)
        q = a + (b - a) * min(start + dash, length) / max(length, 1)
        cv2.line(image, tuple(int(v) for v in p), tuple(int(v) for v in q), colour, width, cv2.LINE_AA)
    return (phase + length) % (2 * dash)

def dotted_line(image, a, b, colour, radius, step=9):
    a, b = np.array(a, float), np.array(b, float)
    length = np.linalg.norm(b - a)
    for d in np.arange(0, length + 1, step):
        p = a + (b - a) * d / max(length, 1)
        cv2.circle(image, tuple(int(v) for v in p), radius, colour, -1, cv2.LINE_AA)

def trail_runs(trail, landed, map_width, respawns=False):
    """A trail cut into runs of one kind of move: [(kind, points)], kind "flight" (skydiving from the dropship,
    before `landed`), "unseen" (lost and picked up again, not seen for a few seconds - one run per gap),
    "respawn" (gone RESPAWN_GAP or more: died and brought back, or every gap in a `respawns` mode, Wildcard,
    where it's back in 5-36 s - one run per gap, not drawn: no line from the death box to the beacon),
    "fast" (faster than on foot, FLY_SPEED, for at least FLY_MIN of the map) or "walk"."""
    runs = []
    for a, b in zip(trail, trail[1:]):
        dt = (point_time(b) - point_time(a)).total_seconds()
        if landed and point_time(b) <= landed:
            kind = "flight"
        elif b.get("gap") or dt > TRAIL_GAP:
            kind = "respawn" if respawns or dt >= RESPAWN_GAP else "unseen"
        elif np.hypot(b["x"] - a["x"], b["y"] - a["y"]) > FLY_SPEED * map_width * max(dt, 1):
            kind = "fast"
        else:
            kind = "walk"
        if runs and runs[-1][0] == kind and kind not in ("unseen", "respawn"):
            runs[-1][1].append(b)
        else:
            runs.append((kind, [a, b]))
    # A short burst is jitter (or a slide): walking
    length = lambda pts: sum(np.hypot(q["x"] - p["x"], q["y"] - p["y"]) for p, q in zip(pts, pts[1:]))
    out = []
    for kind, points in runs:
        if kind == "fast" and length(points) < FLY_MIN * map_width:
            kind = "walk"
        if out and out[-1][0] == kind == "walk":
            out[-1][1].extend(points[1:])
        else:
            out.append((kind, points))
    return out

def ring_stage_check(ring, sizes):
    """None if a ring isn't one of this map's known ring sizes: bigger than the first ring (the largest size:
    it's seen from the dropship every game), or between two sizes that are one stage apart = caught mid-close
    (not kept: where it's going is the next ring). Inside a wider gap (the last rings, too rarely seen to be
    learned) a size is left alone."""
    # (4 px either way too: the last rings are small enough for a few px of measuring to be more than 12%)
    if len(sizes) < 2 or any(abs(ring["r"] - s) <= max(RING_SIZE_MATCH * s, 4) for s in sizes):
        return ring
    bigger = [s for s in sizes if s > ring["r"]]
    smaller = [s for s in sizes if s < ring["r"]]
    if bigger and (not smaller or max(smaller) < STAGE_RATIO * min(bigger)):
        return ring
    return None

def consistent_rings(rings, size=None, sizes=()):
    """Rings only ever shrink, and each next ring sits inside the ring / next ring before it: drop
    sightings that break that (a survey beacon's scan, a dome, a bad fit). `size`: (w, h) of the map
    picture - a ring has to be centred on the map. `sizes`: this map's known ring sizes (ring_stage_check)."""
    out = []
    rings = [r for r in (ring_stage_check(r, sizes) for r in rings) if r]
    # A faint next ring (found inside the ring) has to show up on two screenshots taken at different times
    # (the map stays open a while; two shots of the same second are the same picture)
    same = lambda a, b: abs(a["r"] - b["r"]) <= 0.15 * b["r"] + 3 and np.hypot(a["x"] - b["x"], a["y"] - b["y"]) <= 0.15 * b["r"] + 3
    rings = [r for r in rings if not r.get("faint") or any(o.get("time") != r.get("time") and o["kind"] == "next ring"
                                                           and same(o, r) for o in rings)]
    # A ring seen once can be a red marker circle or a bad fit, and taken first it would block every real ring
    # after it (rings never grow back). So rings seen at two different times go first; a ring seen once only
    # counts if no confirmed ring later on is bigger than it
    backed = lambda r: any(o is not r and o.get("time") != r.get("time") and same(o, r) for o in rings)
    confirmed = [r for r in rings if backed(r)]
    lone = [r for r in rings if not backed(r) and not (r["kind"] == "ring" and any(
        c["kind"] == "ring" and c.get("time", "") > r.get("time", "") and c["r"] > r["r"] * 1.05 for c in confirmed))]
    # A whole circle on two map shots beats a minimap's arc fit: a minimap ring the size of one of those somewhere
    # else is the wrong circle, earlier or later (2026-11-03 00:33: the closing starting circle's edge fitted at the
    # round 1 size, 230 px from the round 1 ring on the map, and kept the map's ring out as a second round 1 ring)
    shots = [r for r in confirmed if r.get("source") != "minimap"]
    elsewhere = lambda r: r.get("source") == "minimap" and any(
        abs(m["r"] - r["r"]) <= 0.15 * m["r"] + 3 and np.hypot(m["x"] - r["x"], m["y"] - r["y"]) > 0.25 * m["r"] + 4
        for m in shots)
    # ...and a next ring on two map shots has to fit inside the ring before it: a minimap ring it sticks out of is an
    # arc fitted in the wrong place (2026-11-03 19:46: E-District's round 3 ring fitted ~60 px off kept the round 4
    # next ring out, seen on 4 map shots right where the closing round 3 hole was). Next rings only: a small "ring"
    # on map shots can be an icon on the tint (Neon Square's, 13 px, on 8 shots that game)
    outside = lambda r: r.get("source") == "minimap" and any(
        m["kind"] == "next ring" and m["r"] < r["r"] * 0.95
        and np.hypot(m["x"] - r["x"], m["y"] - r["y"]) + m["r"] > r["r"] + RING_INSIDE_SLACK * m["r"] + 3 for m in shots)
    wrong = lambda r: elsewhere(r) or outside(r)
    confirmed, lone = [r for r in confirmed if not wrong(r)], [r for r in lone if not wrong(r)]
    for ring in sorted(confirmed + lone, key=lambda r: r.get("time", "")):
        if size and not (-0.05 * size[0] <= ring["x"] <= 1.05 * size[0] and -0.05 * size[1] <= ring["y"] <= 1.05 * size[1]):
            continue
        # A next ring off the minimap needs a ring around it (a lone light disc early on is a dome / scan)
        if ring["kind"] == "next ring" and ring.get("source") == "minimap" and not any(c["kind"] == "ring" for c in out):
            continue
        # Rings never grow back: bigger than a ring already seen means it isn't one
        if any(c["kind"] == "ring" and c["r"] < ring["r"] * 0.95 for c in out):
            continue
        # One round, one place: a circle the size of one already kept (the next ring, then that ring) is there
        # too - a minimap fit of that size somewhere else would be a second round 2 ring
        if any(abs(c["r"] - ring["r"]) <= 0.15 * c["r"] + 3 and np.hypot(c["x"] - ring["x"], c["y"] - ring["y"]) > 0.25 * c["r"] + 4
               for c in out):
            continue
        # Inside every accepted circle at least as big as this one, not just the latest (the first ring seen again
        # after the next ring let a smaller circle outside the next ring through, 2026-10-28). Measured centres
        # wobble a few px: slack, as a share of this circle (a share of the bigger one let the 27.5 px round 5
        # ring stick 9 px out of the 46 px round 4 ring, 2026-11-03 02:10; real ones are out by up to ~17%)
        if any(np.hypot(ring["x"] - c["x"], ring["y"] - c["y"]) + ring["r"] > c["r"] + RING_INSIDE_SLACK * ring["r"] + 3
               for c in out if c["r"] >= ring["r"] * 0.95):
            continue
        # The next ring is the next size down: smaller than every size only once the ring is the smallest (the last
        # ring closes to nothing). A 17 px "next ring" during round 4 was a lighter patch inside it, 2026-11-03 02:07
        if ring["kind"] == "next ring" and sizes and ring["r"] < min(sizes) * (1 - RING_SIZE_MATCH) and any(
                c["kind"] == "ring" for c in out) and min(c["r"] for c in out if c["kind"] == "ring") > min(sizes) * (1 + RING_SIZE_MATCH):
            continue
        out.append(ring)
    return out

def flight_on_view(view):
    """The dropship's flight line (orange chevrons) on a full-map view, as two end points in view px, or None.
    It's only on the map around the jump; a shorter orange streak is lava / an icon, not the flight."""
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    orange = ((hsv[..., 0] >= 5) & (hsv[..., 0] <= 25) & (hsv[..., 1] > 100) & (hsv[..., 2] > 100)).astype(np.uint8) * 255
    found = cv2.HoughLinesP(orange, 1, np.pi / 360, 80, minLineLength=150, maxLineGap=12)
    if found is None:
        return None
    segments = found.reshape(-1, 4).astype(np.float32)
    x1, y1, x2, y2 = max(segments, key=lambda s: np.hypot(s[2] - s[0], s[3] - s[1]))
    d = np.array([x2 - x1, y2 - y1]) / np.hypot(x2 - x1, y2 - y1)
    n = np.array([-d[1], d[0]])
    # Every piece of the same line (the chevrons break it up): within a few px of the longest one
    ends = segments.reshape(-1, 2)
    ends = ends[np.abs((ends - [x1, y1]) @ n) <= 4]
    vx, vy, cx, cy = cv2.fitLine(ends, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
    along = (ends - [cx, cy]) @ np.array([vx, vy])
    if along.max() - along.min() < FLIGHT_MIN_LENGTH * max(view.shape[:2]):
        return None
    return [(cx + vx * along.min(), cy + vy * along.min()), (cx + vx * along.max(), cy + vy * along.max())]

def flight_from_map_screen(locator, screenshot):
    """The dropship's flight line on a full-map screenshot, on the reference: {"x1", "y1", "x2", "y2"} or None."""
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    line = flight_on_view(view)
    if not line:
        return None
    where = locator.locate_map(view)
    if not where or where["inliers"] < 20:
        return None
    (ax, ay), (bx, by) = (where["fit"] @ np.array([x, y, 1.0]) for x, y in line)
    return {"x1": float(ax), "y1": float(ay), "x2": float(bx), "y2": float(by)}

def flight_line(size, heading=None, ship=(), jump=None, seen=None):
    """The dropship's whole flight across the map: a line through where it was seen (dropship minimaps,
    else where you jumped out of it) in the direction it flew (the compass heading on the dropship, else the
    way the dropship minimaps moved), stretched edge to edge of the map picture. `seen`: the flight line from a
    full-map shot (flight_from_map_screen) - the real path, so it sets where the line runs and its exact angle
    when it agrees with that heading (with no heading at all it's used too, but which way it flew is unknown).
    None if not enough to go on. Returns {"start", "end", "heading", "jump"} with points as {"x", "y"} on the
    map picture ("from_map" if the map shot's line was used, "direction_known": False if it flew either way)."""
    pts = np.array([[p["x"], p["y"]] for p in ship], float) if ship else np.empty((0, 2))
    if heading is None and len(pts) >= 2 and np.hypot(*(pts[-1] - pts[0])) >= 15:
        dx, dy = pts[-1] - pts[0]
        heading = float(np.degrees(np.arctan2(dx, -dy)) % 360)
    direction_known = heading is not None
    if seen:
        axis = float(np.degrees(np.arctan2(seen["x2"] - seen["x1"], -(seen["y2"] - seen["y1"]))) % 360)
        if heading is None:
            heading = axis
        else:
            off = (axis - heading) % 360
            off = min(off, 360 - off)
            if off <= FLIGHT_AGREE:
                heading = axis
            elif off >= 180 - FLIGHT_AGREE:
                heading = (axis + 180) % 360
            else:
                seen = None                     # disagrees with the compass: not the flight line
    if heading is None:
        return None
    if seen:
        anchor = np.array([(seen["x1"] + seen["x2"]) / 2, (seen["y1"] + seen["y2"]) / 2])
    elif len(pts):
        anchor = np.median(pts, axis=0)
    elif jump:
        anchor = np.array([jump["x"], jump["y"]], float)
    else:
        return None
    d = np.array([np.sin(np.radians(heading)), -np.cos(np.radians(heading))])
    w, h = size
    # How far the line runs each way before leaving the picture
    ts = []
    for axis, limit in ((0, w), (1, h)):
        if abs(d[axis]) > 1e-9:
            ts += [(0 - anchor[axis]) / d[axis], (limit - anchor[axis]) / d[axis]]
    inside = [t for t in ts if all(-1 <= (anchor + t * d)[i] <= (w, h)[i] + 1 for i in (0, 1))]
    if len(inside) < 2:
        return None
    a, b = anchor + min(inside) * d, anchor + max(inside) * d
    out = {"start": {"x": float(a[0]), "y": float(a[1])}, "end": {"x": float(b[0]), "y": float(b[1])},
           "heading": int(round(heading)) % 360}
    if seen:
        out["from_map"] = True
    if not direction_known:
        out["direction_known"] = False
    if jump:
        # Where you left it: your first spot after jumping, put back onto the line
        j = np.array([jump["x"], jump["y"]], float)
        on = anchor + np.dot(j - anchor, d) * d
        out["jump"] = {"x": float(on[0]), "y": float(on[1]), "time": jump.get("time", "")}
    return out

def best_flight(size, heading=None, ship=(), jump=None, seen=()):
    """flight_line() through the first flight line from a map shot (`seen`) that agrees with the heading, else
    from the heading and the dropship minimaps alone."""
    for line in seen:
        flight = flight_line(size, heading, ship, jump, seen=line)
        if flight and flight.get("from_map"):
            return flight
    return flight_line(size, heading, ship, jump)

def draw_flight(image, flight, P, zoomed):
    """Dashed line with direction chevrons, and a marker where you jumped."""
    colour = (250, 200, 120)
    a, b = np.array(P(flight["start"]), float), np.array(P(flight["end"]), float)
    length = np.linalg.norm(b - a)
    if length < 2:
        return
    d = (b - a) / length
    n = np.array([-d[1], d[0]])
    dashed_line(image, tuple(a.astype(int)), tuple(b.astype(int)), colour, 3 if zoomed else 2, dash=14)
    size = 16 if zoomed else 11
    for t in np.arange(60, length - 30, 160) if flight.get("direction_known", True) else ():
        tip = a + d * t
        for side in (1, -1):
            tail = tip - d * size + n * size * 0.7 * side
            cv2.line(image, tuple(tip.astype(int)), tuple(tail.astype(int)), colour, 3 if zoomed else 2, cv2.LINE_AA)
    if flight.get("jump"):
        j = P(flight["jump"])
        cv2.circle(image, j, 9, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(image, j, 3, colour, -1, cv2.LINE_AA)

def ring_label(ring, locator=None):
    """"Ring 3 02:07": the round from the ring's size (MapLocator.ring_round), not the timer's - the timer counts down to
    the next round, so over round 1's ring it says ROUND 2 (every first ring was labelled "Ring 2" until 2026-11-05)."""
    name = {"ring": "Ring", "next ring": "Next ring"}[ring["kind"]]
    stage = locator.ring_round(ring["r"])[0] if locator and ring["kind"] == "ring" else None
    if stage:
        name = f"Ring {stage}"
    return f"{name} {ring.get('time', '')[11:16]}".strip()

def distinct_rings(rings):
    """The same ring seen several times (or as "next ring", then as the ring) is drawn once: where its
    sightings agree (median), labelled as the latest one. Sightings of one ring measure up to ~15% apart;
    two different rings of a match are never that close in both size and place. Once the ring's red edge has
    been seen on a map shot, only those sightings place it: the next ring's lighter disc has a soft edge (Storm
    Point's round 2 ring as a next ring, 2026-11-03 23:32: 3 px high and 2 small, 5 px off at its bottom edge;
    its edge on the map shots: within 1 px)."""
    groups = []
    for ring in sorted(rings, key=lambda r: r.get("time", "")):
        for g in groups:
            mid = {k: float(np.median([m[k] for m in g])) for k in ("x", "y", "r")}
            if abs(mid["r"] - ring["r"]) <= 0.15 * mid["r"] + 3 and np.hypot(mid["x"] - ring["x"], mid["y"] - ring["y"]) <= 0.25 * mid["r"] + 4:
                g.append(ring)
                break
        else:
            groups.append([ring])
    sharp = lambda g: [m for m in g if m["kind"] == "ring" and m.get("source") != "minimap"] or g
    return [dict(g[-1], **{k: float(np.median([m[k] for m in sharp(g)])) for k in ("x", "y", "r")}) for g in groups]

def font(size):
    from PIL import ImageFont
    for path in FONT_FILES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def base_map(locator, region=None):
    """The reference (or the `region` x1, y1, x2, y2 of it), with the stitched minimaps laid over it
    wherever they've been seen. Map icons (beacons, Replicators...) are left out: the reference is a copy with
    them painted over, and where the stitched map has one the reference shows instead (Map_Icons).
    Returns (image, zoom, seen, offset): picture px = (ref - offset) * zoom."""
    import Map_Icons
    ref = locator.reference
    if (locator.folder / "reference.png").exists():
        clean = cv2.imread(str(Map_Icons.drawing_reference(locator.folder)))
        ref = clean if clean is not None and clean.shape == ref.shape else ref
    h, w = ref.shape[:2]
    x1, y1, x2, y2 = (int(round(v)) for v in (region or (0, 0, w, h)))
    zoom = OUTPUT_SIZE / max(x2 - x1, y2 - y1)
    size = (int(round((x2 - x1) * zoom)), int(round((y2 - y1) * zoom)))
    image = cv2.resize(ref[y1:y2, x1:x2], size, interpolation=cv2.INTER_CUBIC).astype(np.float32)
    seen = np.zeros(size[::-1], np.float32)
    stitched = locator.folder / "stitched.png"
    coverage = locator.folder / "coverage.png"
    if stitched.exists() and coverage.exists() and locator.stitch_zoom:
        # The stitched map is at the minimap's own resolution: the zoomed view gets its full detail
        z = locator.stitch_zoom
        sx1, sy1, sx2, sy2 = (int(round(v * z)) for v in (x1, y1, x2, y2))
        sharp = cv2.resize(cv2.imread(str(stitched))[sy1:sy2, sx1:sx2], size, interpolation=cv2.INTER_CUBIC).astype(np.float32)
        count = cv2.resize(cv2.imread(str(coverage), cv2.IMREAD_GRAYSCALE)[sy1:sy2, sx1:sx2], size, interpolation=cv2.INTER_LINEAR)
        # Fade the stitched minimaps in (no hard rectangle edges), fully once seen a few times
        seen = np.minimum(count / 3.0, 1.0).astype(np.float32)
        # Alter's ultimate got stitched in from some minimaps: show the plain map picture there instead
        alter_ult = cv2.dilate(violet_mask(sharp.astype(np.uint8)), np.ones((7, 7), np.uint8))
        seen[alter_ult > 0] = 0
        icons = cv2.imread(str(Map_Icons.stitched_icons(locator.folder)), cv2.IMREAD_GRAYSCALE)
        if icons is not None:
            seen[cv2.resize(icons[sy1:sy2, sx1:sx2], size, interpolation=cv2.INTER_NEAREST) > 0] = 0
        seen = cv2.GaussianBlur(seen, (0, 0), 6)
        image = image * (1 - seen[..., None]) + sharp * seen[..., None]
    # Light maps get a little darker so trails stand out; dark maps (Worlds Edge) get brighter instead
    gain = float(np.clip(85 / max(image.mean(), 1), 0.8, 1.6))
    return np.clip(image * gain, 0, 255).astype(np.uint8), zoom, seen, (x1, y1)

def zoom_region(locator, points):
    """A square box around everything that happened (trails, landing, death), padded, on the map."""
    h, w = locator.reference.shape[:2]
    xs, ys = [p["x"] for p in points], [p["y"] for p in points]
    if not xs:
        return None
    side = max(max(xs) - min(xs), max(ys) - min(ys)) * (1 + 2 * ZOOM_PADDING)
    side = min(max(side, ZOOM_MIN_SIZE * w), w, h)
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    x1 = min(max(cx - side / 2, 0), w - side)          # slide the box back onto the map, don't cut it
    y1 = min(max(cy - side / 2, 0), h - side)
    return (x1, y1, x1 + side, y1 + side)

def point_time(p):
    from datetime import datetime
    return p["when"] if "when" in p else datetime.fromisoformat(p["time"])

def draw_item(image, at, kind, size):
    """A Care Package (dark diamond, white edge and dot), a Stronghold (pink-red octagon, white edge) or an Evo
    Harvester (dark diamond, yellow edge and up arrows), like the game's map icons. `size`: half its width in px."""
    x, y = at
    if kind == "care_package":
        corners = np.array([(x, y - size), (x + size, y), (x, y + size), (x - size, y)], np.int32)
        cv2.fillPoly(image, [corners], (35, 35, 35), cv2.LINE_AA)
        cv2.polylines(image, [corners], True, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(image, (x, y), max(2, size // 3), (255, 255, 255), -1, cv2.LINE_AA)
    elif kind == "stronghold":
        corners = np.array([(x + size * np.cos(a), y + size * np.sin(a)) for a in np.arange(8) * np.pi / 4 + np.pi / 8], np.int32)
        cv2.fillPoly(image, [corners], STRONGHOLD_DRAW, cv2.LINE_AA)
        cv2.polylines(image, [corners], True, (255, 255, 255), 1, cv2.LINE_AA)
        r = max(2, size // 3)
        cv2.rectangle(image, (x - r, y - r), (x + r, y + r), (40, 20, 90), -1)
    elif kind == "evo_harvester":
        corners = np.array([(x, y - size), (x + size, y), (x, y + size), (x - size, y)], np.int32)
        cv2.fillPoly(image, [corners], (35, 35, 35), cv2.LINE_AA)
        cv2.polylines(image, [corners], True, EVO_DRAW, 2, cv2.LINE_AA)
        for lift in (-0.38, 0.12):            # two chevrons pointing up (thin on a small icon, or they run together)
            w, top = size * 0.4, y + size * lift
            arrow = np.array([(x - w, top + w * 0.6), (x, top), (x + w, top + w * 0.6)], np.int32)
            cv2.polylines(image, [arrow], False, EVO_DRAW, 1 if size < 14 else 2, cv2.LINE_AA)

def render(locator, out_file, me_trail, mates=None, ship=None, rings=(), landing=None, death=None,
           header="", my_colour_name=None, names=None, base=None, region=None, downs=(), scans=(), items=(),
           respawns=False):
    """Draw the match picture. Trails are lists of {"x", "y"} on the reference. `base`: a base_map()
    result to reuse (the live map redraws often). `region`: draw just that part of the map (zoomed).
    `items`: Map_Icons items; Care Packages and Strongholds are drawn (Map_Icons.DRAWN), the rest are SVG only."""
    import Map_Icons
    from PIL import Image, ImageDraw
    image, zoom, seen, (ox, oy) = base if base else base_map(locator, region)
    image = image.copy()
    zoomed = region is not None
    P = lambda p: (int((p["x"] - ox) * zoom), int((p["y"] - oy) * zoom))
    rings = distinct_rings(rings)
    for ring in rings:
        # Ring: solid white; next ring: thinner, light grey
        colour, width = ((255, 255, 255), 3) if ring["kind"] == "ring" else ((190, 190, 190), 2)
        cv2.circle(image, P(ring), int(ring["r"] * zoom), colour, width, cv2.LINE_AA)
    for scan in scans:
        # Survey beacon scan: dotted teal circle, pin in the middle
        r = int(scan["r"] * zoom)
        for start in range(0, 360, 6):
            cv2.ellipse(image, P(scan), (r, r), 0, start, start + 3, SCAN_DRAW, 2, cv2.LINE_AA)
    if ship:
        draw_flight(image, ship, P, zoomed)
    if death:
        # Under the trails, so the squad's last moves stay visible on top of it
        cv2.drawMarker(image, P(death), (60, 60, 255), cv2.MARKER_TILTED_CROSS, 26, 3)
    # You first, teammates on top: squads move together, and a teammate drawn under your thicker line
    # disappears. Each teammate is also nudged a few px sideways so shared paths show side by side.
    trails = [(my_colour_name or "me", me_trail, 6 if zoomed else 5, (0, 0))]
    nudge = 4 if zoomed else 3
    for i, (name, trail) in enumerate((mates or {}).items()):
        trails.append((name, trail, 4 if zoomed else 3, (nudge, -nudge) if i % 2 == 0 else (-nudge, nudge)))
    landed = point_time(landing) if landing else None
    plain_P = P
    for colour, trail, width, (nx, ny) in trails:
        P = (lambda q, nx=nx, ny=ny: (plain_P(q)[0] + nx, plain_P(q)[1] + ny))
        draw = SQUAD_COLOURS.get(colour, {}).get("draw", (255, 255, 255))
        for kind, points in trail_runs(trail, landed, locator.reference.shape[1], respawns):
            if kind == "respawn":
                continue                  # died and came back: the trail just picks up again at the beacon
            if kind == "unseen":
                # Lost and picked up again (not seen for a few seconds): dotted, the way they went in between
                # isn't known
                dotted_line(image, P(points[0]), P(points[1]), draw, 3 if zoomed else 2)
                continue
            if kind in ("flight", "fast"):
                # Skydiving from the dropship, or faster than on foot (jump tower, balloon, Loba's teleport):
                # not walking there, so a thin dashed line
                phase = 0.0
                for a, b in zip(points, points[1:]):
                    phase = dashed_line(image, P(a), P(b), draw, 2, phase=phase)
                continue
            for a, b in zip(points, points[1:]):
                # The teammate you were watching while dead: as bold as your own trail
                bold = a.get("watched") and b.get("watched")
                cv2.line(image, P(a), P(b), draw, (6 if zoomed else 5) if bold else width, cv2.LINE_AA)
        if trail and colour != (my_colour_name or "me"):
            cv2.drawMarker(image, P(trail[-1]), draw, cv2.MARKER_TILTED_CROSS, 18, 3)
    P = plain_P                                   # markers below sit exactly where things happened
    for item in items:                     # on top of the trails: a squad that went there would hide it
        if item["kind"] in Map_Icons.DRAWN:
            draw_item(image, P(item), item["kind"], 16 if zoomed else 11)
    for scan in scans:
        # Pin in the middle of each survey beacon scan, on top of the trails
        cx, cy = P(scan)
        size = 1.4 if zoomed else 1.0
        tip, head = (cx, cy), (cx, int(cy - 22 * size))
        cv2.line(image, tip, head, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.circle(image, head, int(9 * size), SCAN_DRAW, -1, cv2.LINE_AA)
        cv2.circle(image, head, int(9 * size), (255, 255, 255), 2, cv2.LINE_AA)
    ticks = []
    if zoomed and me_trail:
        # A dot on your trail every minute after the jump
        start, minute, prev = point_time(me_trail[0]), 1, None
        for p in me_trail:
            elapsed = (point_time(p) - start).total_seconds()
            if prev is not None and (point_time(p) - point_time(prev)).total_seconds() > TRAIL_GAP:
                minute = int(elapsed // 60) + 1    # minutes you were down for don't get a dot
            elif elapsed >= 60 * minute:
                cv2.circle(image, P(p), 7, (255, 255, 255), -1, cv2.LINE_AA)
                cv2.circle(image, P(p), 7, (0, 0, 0), 2, cv2.LINE_AA)
                ticks.append((P(p), f"{minute}m"))
                minute = int(elapsed // 60) + 1
            prev = p
    if landing:
        cv2.circle(image, P(landing), 14, (80, 255, 80), 3, cv2.LINE_AA)
    labels = []
    for d in downs:
        # Went down here (small red X) ... came back here (green ring)
        cv2.drawMarker(image, P(d["down"]), (60, 60, 255), cv2.MARKER_TILTED_CROSS, 22, 4)
        cv2.circle(image, P(d["back"]), 11, (80, 255, 80), 3, cv2.LINE_AA)
        labels += [(P(d["down"]), f"down {d['down']['time'][11:16]}"), (P(d["back"]), f"back {d['back']['time'][11:16]}")]

    pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    small, big = font(18), font(26)
    if ship and ship.get("jump"):
        jx, jy = P(ship["jump"])
        draw.text((jx + 12, jy - 22), "jumped" + (f" {ship['jump']['time'][11:16]}" if ship["jump"].get("time") else ""),
                  font=small, fill=(250, 220, 170), stroke_width=3, stroke_fill=(0, 0, 0))
    for (x, y), label in labels:
        draw.text((x + 14, y + 4), label, font=small, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    for (x, y), label in ticks:
        draw.text((x + 10, y - 10), label, font=small, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    for ring in rings:
        if ring.get("time"):
            x, y = P(ring)
            draw.text((x, y - int(ring["r"] * zoom) - 14), ring_label(ring, locator), font=small, anchor="mm",
                      fill=(255, 255, 255) if ring["kind"] == "ring" else (200, 200, 200),
                      stroke_width=3, stroke_fill=(0, 0, 0))
    for poi in locator.pois:
        x, y = P(poi)
        if not (0 <= y < seen.shape[0] and 0 <= x < seen.shape[1]):
            continue
        if not zoomed and seen[y, x] < 0.5:
            continue                              # the map picture's own label is still showing there
        draw.text((x, y), poi["name"].upper(), font=big if zoomed else small, fill=(235, 235, 235), anchor="mm",
                  stroke_width=3, stroke_fill=(0, 0, 0))
    lines = [header] if header else []
    for colour, name in (names or {}).items():
        lines.append(f"\u25a0 {name}")
    if lines:
        y = 14
        for i, text in enumerate(lines):
            fill = (255, 255, 255)
            if i and names:
                bgr = SQUAD_COLOURS[list(names)[i - 1]]["draw"]
                fill = (bgr[2], bgr[1], bgr[0])
            draw.text((16, y), text, font=big if i == 0 else small, fill=fill, stroke_width=3, stroke_fill=(0, 0, 0))
            y += 34 if i == 0 else 24
    image = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    save_png(image, out_file)

def smooth(trail):
    """Rolling median of a teammate's trail (between gaps): one-off jumps to a nearby ping go away."""
    out, segment = [], []
    for p in trail + [None]:
        if p is None or (p.get("gap") and segment):
            k = SMOOTH_WINDOW // 2
            for i, q in enumerate(segment):
                window = segment[max(0, i - k): i + k + 1]
                out.append(dict(q, x=float(np.median([w["x"] for w in window])), y=float(np.median([w["y"] for w in window]))))
            segment = []
        if p is not None:
            segment.append(p)
    return out

def render_both(locator, out_file, zoom_file, me_trail, mates=None, landing=None, death=None, base=None, downs=(), scans=(), **kwargs):
    """The whole map, and the zoomed view around where the squad went after landing."""
    mates = {c: smooth(t) for c, t in (mates or {}).items()}
    render(locator, out_file, me_trail, mates=mates, landing=landing, death=death, base=base, downs=downs, scans=scans, **kwargs)
    # Frame what happened on the ground; the skydive just runs in from the edge
    landed = point_time(landing) if landing else None
    after = (lambda t: [p for p in t if point_time(p) >= landed]) if landed else (lambda t: list(t))
    points = after(me_trail) + [p for t in mates.values() for p in after(t)] + [p for p in (landing, death) if p]
    region = zoom_region(locator, points)
    if region:
        render(locator, zoom_file, me_trail, mates=mates, landing=landing, death=death, region=region, downs=downs,
               scans=scans, **kwargs)

def svg_colour(colour):
    b, g, r = SQUAD_COLOURS.get(colour, {}).get("draw", (255, 255, 255))
    return f"#{r:02x}{g:02x}{b:02x}"

def write_svg(locator, out_file, players, rings=(), ship=None, landing=None, death=None, title="", map_href=None, downs=(), scans=(),
              items=(), respawns=False):
    landed_at = landing.get("time") if landing else None
    """The match as vector layers on the map picture's own coordinates, to zoom in as far as you like or
    lay over the map in another program. `players`: [{"name", "colour", "is_me", "points": [{"time", "x", "y"}]}].
    Layers (SVG groups): map, rings, scans, items (one group per kind: Care Packages, Evo Harvesters, Replicators,
    Respawn Beacons, Ring Consoles), dropship, events, players (one group each), pois."""
    from datetime import datetime
    from html import escape
    h, w = locator.reference.shape[:2]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
           f"<title>{escape(title)}</title>",
           "<style>.line{fill:none;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}"
           "text{font-family:sans-serif;font-weight:bold}"
           ".halo{stroke:#000;stroke-width:3px;stroke-linejoin:round;vector-effect:non-scaling-stroke}</style>"]
    if map_href:
        out.append(f'<g id="map"><image href="{escape(map_href)}" x="0" y="0" width="{w}" height="{h}"/></g>')
    out.append('<g id="rings">')
    for ring in distinct_rings(rings):
        colour = "#ffffff" if ring["kind"] == "ring" else "#bebebe"
        # Which round it is and its size in game metres (Map_Locate.RING_RADII_M), from the map's ring sizes
        stage, metres = locator.ring_round(ring["r"])
        metres = "" if metres is None else str(metres)
        out.append(f'<circle class="line" cx="{ring["x"]:.1f}" cy="{ring["y"]:.1f}" r="{ring["r"]:.1f}" stroke="{colour}" '
                   f'stroke-width="2" data-kind="{ring["kind"]}" data-time="{ring.get("time", "")}" '
                   f'data-round="{ring.get("round") or ""}" data-source="{ring.get("source", "map screen")}" '
                   f'data-radius-m="{metres}" data-stage="{stage or ""}">'
                   f'<title>{escape(ring_label(ring))}{f" - round {stage}" if stage else ""}'
                   f'{f", {metres} m radius" if metres else ""}</title></circle>')
    out.append("</g>")
    out.append('<g id="scans">')
    for scan in scans:
        out.append(f'<circle class="line" cx="{scan["x"]:.1f}" cy="{scan["y"]:.1f}" r="{scan["r"]:.1f}" stroke="#3cd2c8" '
                   f'stroke-width="1" stroke-dasharray="2 6" opacity="0.7"><title>Survey scan {scan.get("time", "")[11:]}</title></circle>')
        out.append(f'<circle cx="{scan["x"]:.1f}" cy="{scan["y"]:.1f}" r="3" fill="#3cd2c8" stroke="#ffffff" stroke-width="1">'
                   f'<title>Survey scan {scan.get("time", "")[11:]}</title></circle>')
    out.append("</g>")
    import Map_Icons
    out.append('<g id="items">')
    for kind, (label, colour) in Map_Icons.ITEMS.items():
        mine = [i for i in items if i["kind"] == kind]
        out.append(f'<g id="items-{kind}" data-label="{label}s" data-count="{len(mine)}">')
        for i in mine:
            x, y, r = i["x"], i["y"], 4
            out.append(f'<path d="M{x:.1f},{y - r:.1f} L{x + r:.1f},{y:.1f} L{x:.1f},{y + r:.1f} L{x - r:.1f},{y:.1f} Z" '
                       f'fill="{colour}" stroke="#000" stroke-width="1" data-first-seen="{i["first_seen"]}" data-seen="{i["seen"]}">'
                       f'<title>{label} (first seen {i["first_seen"][11:19]}, {i["seen"]} sightings)</title></path>')
        out.append("</g>")
    out.append("</g>")
    if ship:
        a, b = ship["start"], ship["end"]
        out.append('<defs><marker id="ship-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="5" '
                   'markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10" fill="none" '
                   'stroke="#78c8fa" stroke-width="2"/></marker></defs>')
        out.append(f'<g id="dropship" data-heading="{ship["heading"]}"><title>Dropship {ship["heading"]}\u00b0</title>'
                   f'<line class="line" x1="{a["x"]:.1f}" y1="{a["y"]:.1f}" x2="{b["x"]:.1f}" y2="{b["y"]:.1f}" '
                   f'stroke="#78c8fa" stroke-width="2" stroke-dasharray="8 6"'
                   + (' marker-mid="url(#ship-arrow)" marker-end="url(#ship-arrow)"' if ship.get("direction_known", True) else "")
                   + '/>')
        if ship.get("jump"):
            j = ship["jump"]
            out.append(f'<circle class="line" cx="{j["x"]:.1f}" cy="{j["y"]:.1f}" r="4" stroke="#ffffff" '
                       f'stroke-width="2"><title>Jumped {j.get("time", "")[11:]}</title></circle>')
        out.append("</g>")
    # Landing / downs / death sit under the paths, so the last moments of the squad stay visible
    out.append('<g id="events">')
    if landing:
        out.append(f'<circle class="line" cx="{landing["x"]:.1f}" cy="{landing["y"]:.1f}" r="5" stroke="#50ff50" '
                   f'stroke-width="2"><title>Landed {landing.get("poi", "")} {landing.get("time", "")[11:]}</title></circle>')
    for d in downs:
        out.append(f'<circle class="line" cx="{d["down"]["x"]:.1f}" cy="{d["down"]["y"]:.1f}" r="3" stroke="#ff3c3c" '
                   f'stroke-width="2"><title>Down {d["down"]["time"][11:]}</title></circle>')
        out.append(f'<circle class="line" cx="{d["back"]["x"]:.1f}" cy="{d["back"]["y"]:.1f}" r="3" stroke="#50ff50" '
                   f'stroke-width="2"><title>Back {d["back"]["time"][11:]}</title></circle>')
    if death:
        x, y = death["x"], death["y"]
        out.append(f'<g stroke="#ff3c3c" stroke-width="2" opacity="0.8" class="line"><title>Died {death.get("zone", "")} '
                   f'{death.get("time", "")[11:]}</title><line x1="{x-5:.1f}" y1="{y-5:.1f}" x2="{x+5:.1f}" y2="{y+5:.1f}"/>'
                   f'<line x1="{x-5:.1f}" y1="{y+5:.1f}" x2="{x+5:.1f}" y2="{y-5:.1f}"/></g>')
    out.append("</g>")
    out.append('<g id="players">')
    for player in players:
        colour, pts = svg_colour(player["colour"]), player["points"]
        safe = "".join(c if c.isalnum() else "-" for c in player["name"])
        out.append(f'<g id="player-{safe}" data-name="{escape(player["name"])}" data-colour="{player["colour"]}" '
                   f'data-is-me="{str(player["is_me"]).lower()}">')
        landed = datetime.fromisoformat(landed_at) if landed_at else None
        for kind, segment in trail_runs(pts, landed, w, respawns):
            # Walking solid; skydiving / faster than on foot dashed; lost and picked up again dotted; died and
            # came back nothing (the trail starts again where they're back)
            if kind == "respawn":
                continue
            coords = " ".join(f'{q["x"]:.1f},{q["y"]:.1f}' for q in segment)
            times = ",".join(q["time"][11:19] for q in segment)
            watched = all(q.get("watched") for q in segment)
            style = {"flight": ' stroke-dasharray="6 6"', "fast": ' stroke-dasharray="6 6"',
                     "unseen": ' stroke-dasharray="0 6"'}.get(kind, "")
            width = 1 if kind in ("flight", "fast") else 3 if (player["is_me"] or watched) and kind == "walk" else 2
            out.append(f'<polyline class="line {kind}{" watched" if watched else ""}" '
                       f'points="{coords}" stroke="{colour}" stroke-width="{width}"{style} data-times="{times}"/>')
        if pts:
            start, minute = pts[0]["time"], 1
            from datetime import datetime
            t0 = datetime.fromisoformat(start)
            for q in pts:
                if (datetime.fromisoformat(q["time"]) - t0).total_seconds() >= 60 * minute:
                    out.append(f'<circle cx="{q["x"]:.1f}" cy="{q["y"]:.1f}" r="1.5" fill="{colour}">'
                               f'<title>{escape(player["name"])} {minute}m ({q["time"][11:19]})</title></circle>')
                    minute += 1
        out.append("</g>")
    out.append("</g>")
    out.append('<g id="pois">')
    for poi in locator.pois:
        # Outline first, then the letters on top (renders the same in every viewer)
        for cls, fill in (("halo", "#000"), ("", "#ebebeb")):
            out.append(f'<text{f" class={chr(34)}{cls}{chr(34)}" if cls else ""} x="{poi["x"]:.1f}" y="{poi["y"]:.1f}" '
                       f'font-size="{w / 90:.1f}" fill="{fill}" text-anchor="middle">{escape(poi["name"].upper())}</text>')
    out.append("</g></svg>")
    Path(out_file).write_text("\n".join(out))

def save_png(image, path):
    """Write then rename, so OBS never reads a half-written file. The temp name is per process: the live map and
    Extract_Stats (publishing the match before) once wrote the same one at once, and one rename found it gone."""
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.tmp.png")
    cv2.imwrite(str(tmp), image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    os.replace(tmp, path)

def publish(match_picture):
    """Make this match's pictures the "Latest Match Map.png" / "... Zoomed.png" OBS shows."""
    save_png(cv2.imread(str(match_picture)), LATEST_MAP)
    zoomed = Path(match_picture).with_name("match_map_zoom.png")
    if zoomed.exists():
        save_png(cv2.imread(str(zoomed)), LATEST_ZOOM)

# ---------------------------
# LIVE MAP (during a match)
# ---------------------------

class LiveMap:
    """Keeps "Latest Match Map.png" showing the match in progress. Stat_Capture hands it every minimap
    and map screenshot; the work happens on a background thread so capturing never waits for it."""

    def __init__(self):
        self.jobs = queue.Queue(maxsize=120)
        self.map_seen, self.map_queued, self.map_open_shots = None, None, 0   # (capture thread) map shots handed on
        self.map_opening, self.map_spare = 0, None   # which time the map's open; the newest shot not handed on
        self.locators = {}                       # map name -> MapLocator (kept: loading one takes seconds)
        self.dropped = 0                         # jobs skipped because the queue was full (logged when behind)
        self.last_map = None
        self.clear()
        threading.Thread(target=self.run, daemon=True, name="live-map").start()

    def clear(self):
        self.locator = None
        self.tries = {}                          # map name -> good matches so far, while working out the map
        self.attempts = 0
        self.path, self.ship, self.rings, self.scans = [], [], [], []
        self.jumped = []                         # placements too far to have walked, waiting to be confirmed
        self.votes, self.mine, self.tracker = {}, None, None
        self.last = None                         # (when, minimap, fit, where) of the last placed minimap
        self.unplaced = []                       # (minimap, when, screen) not placed since then, to line up backwards
        self.ring_samples = []                   # (when, ring edge points) from the minimaps
        from Map_Locate import RingTimer
        self.timer = RingTimer()                 # the ring timer under the minimap: closing or not
        self.minimap_rings = []                  # rings found from them so far
        self.prior = None                        # Map_Locate.ring_prior() of the rings drawn last
        self.panel_names = {}                    # colour -> {name: times read next to that colour's frame}
        self.last_on_panel, self.missing = {}, {}    # colour -> when last read on the squad panel / reads since
        self.other_squad = None                  # spectating: another squad (True), yours (False), not known yet (None)
        self.undecided = []                      # (minimap, when, where) spectated while that's not known yet
        self.base, self.dirty, self.drawn = None, False, 0.0
        # Seen before the map was worked out (the dropship, mostly): placed once it is, so its flight isn't lost
        self.early_minimaps, self.early_maps = [], []
        self.flights_seen = []                   # the dropship's flight line on full-map shots (flight_from_map_screen)
        self.rings_written = None                # (rings, your spot) last put in LIVE_RINGS
        self.rings_said = None                   # ...and as last logged
        self.map_found = {}                      # opening of the map -> [shots that showed a ring, skipped shots taken]
        self.item_sightings, self.item_minimaps = [], 0   # Care Packages / Strongholds seen (Map_Icons.DRAWN)
        self.items_looked = {}                   # map opening -> shots its items were looked for on

    # ---- called from the capture loop (never blocks) ----
    def new_match(self):
        self._put(("new",))

    def catch_up(self, folder):
        """Restarted mid-match: replay what's been saved for it so far, so the picture carries on."""
        self._put(("catch up", Path(folder)))

    def add_minimap(self, image, when, screen):
        self._put(("minimap", image.copy(), when, screen))

    def add_map_screen(self, image, when):
        # The map open is saved every 2 s, and each shot costs the live map ~3 s (the ring search): with the map up
        # half a minute it fell 20+ s behind and never showed the next ring (2026-11-03 19:34). Each time the map's
        # opened its first two shots (a faint next ring needs two), then one every LIVE_MAP_SHOT_EVERY s
        if self.map_seen is None or (when - self.map_seen).total_seconds() > LIVE_MAP_REOPEN:
            self.map_open_shots = 0
            self.map_opening += 1
        self.map_seen = when
        self.map_open_shots += 1
        if self.map_open_shots > 2 and self.map_queued and (when - self.map_queued).total_seconds() < LIVE_MAP_SHOT_EVERY:
            self.map_spare = (self.map_opening, image.copy(), when)     # (map_shot may still want it)
            return
        self.map_queued = when
        self._put(("map", image.copy(), when, self.map_opening))

    def add_squad_panel(self, image, when, screen="in_game"):
        self._put(("squad", image.copy(), when, screen))

    def add_ring_timer(self, image, when):
        self._put(("timer", image.copy(), when))

    def _put(self, job):
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            self.dropped += 1                    # falling behind: skip a frame rather than stall capture

    # ---- the worker ----
    def run(self):
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "new":
                    self.clear()
                elif job[0] == "catch up":
                    self.replay(job[1])
                elif job[0] == "minimap":
                    self.minimap(*job[1:])
                elif job[0] == "timer":
                    self.timer.add(job[2], job[1])
                elif job[0] == "squad":
                    self.squad_panel(job[1], job[2], job[1].shape[1] / (SQUAD_PANEL[2] - SQUAD_PANEL[0]), job[3])
                elif job[0] == "map" and not self.locator:
                    if len(self.early_maps) < 3:          # (full frames: only the first few, from the dropship)
                        self.early_maps.append(job[1:3])
                elif job[0] == "map":
                    self.map_shot(*job[1:])
                waited = time.monotonic() - self.drawn
                # Drawn when caught up - or, behind, at least every LIVE_STALL s (the rings come out of the drawing too)
                if self.dirty and waited >= LIVE_EVERY and (self.jobs.qsize() < 5 or waited >= LIVE_STALL):
                    if self.jobs.qsize() >= 5:
                        print(f"[live map] behind: {self.jobs.qsize()} jobs waiting, {self.dropped} dropped so far")
                    self.draw()
            except Exception:
                traceback.print_exc()

    def write_rings(self, rings):
        """Captures/live_rings.json: this match's rings so far and where you are (when the rings change or you've
        moved 15 px), written then swapped in."""
        import json
        from datetime import datetime
        keep = [{k: ring[k] for k in ("kind", "x", "y", "r", "time") if k in ring} for ring in distinct_rings(rings)]
        me = {"x": round(self.path[-1]["x"], 1), "y": round(self.path[-1]["y"], 1)} if self.path else None
        last = self.rings_written or (None, None)
        moved = me and (not last[1] or np.hypot(me["x"] - last[1]["x"], me["y"] - last[1]["y"]) >= 15)
        if keep == last[0] and not moved:
            return
        said = ", ".join(f"{r['kind']} {r['r']:g}" for r in keep) or "none"
        if said != self.rings_said:              # (a line in capture.log each time the rings change)
            print(f"[live map] rings: {said}")
            self.rings_said = said
        me = me if moved else last[1]
        try:
            tmp = LIVE_RINGS.with_name(".live_rings.json.tmp")
            tmp.write_text(json.dumps({"map": self.locator.map_name, "time": datetime.now().isoformat(timespec="seconds"),
                                       "rings": keep, "me": me}, default=float))
            os.replace(tmp, LIVE_RINGS)
            self.rings_written = (keep, me)
        except OSError:
            pass

    def map_shot(self, image, when, opening):
        """A map shot handed on, then - while this opening of the map hasn't shown a ring on 2 shots (a faint next
        ring needs two) - the newest one add_map_screen skipped since, up to LIVE_MAP_RETRY of them. The second
        shot of an opening can show nothing (zooming in, or not placed): 2026-11-05 01:47:33-01:47:41, round 5's
        next ring was on the 1st and 6th shots, the 2nd showed nothing and the 3rd-6th were skipped, so it was
        seen once and never drawn until it had closed 2 min later."""
        found = self.map_found.setdefault(opening, [0, 0])
        while True:
            found[0] += self.map_screen(image, when, opening)
            spare = self.map_spare
            if found[0] >= 2 or found[1] >= LIVE_MAP_RETRY or not spare or spare[0] != opening or spare[2] <= when:
                return
            self.map_spare = None
            found[1] += 1
            _, image, when = spare

    def map_screen(self, image, when, opening=None):
        """Rings, scans and squad arrows off one map shot. True if it showed a ring. Items only on the first
        LIVE_ITEM_SHOTS shots of an opening of the map that line up: they don't move, looking costs ~1.9 s a shot, and
        two sightings make an item (the orange Care Package circle is only on the full map)."""
        self.flight_shot(image, when)
        from Map_Locate import rings_on_map_screen
        rings = rings_on_map_screen(self.locator, image, self.prior)
        self.rings += [dict(r, time=when.isoformat(timespec="seconds")) for r in rings]
        if opening is None or self.items_looked.get(opening, 0) < LIVE_ITEM_SHOTS:
            import Map_Icons
            items = Map_Icons.map_sightings(self.locator, image, when, Map_Icons.DRAWN)
            if items is not None:
                self.item_sightings += items
                self.items_looked[opening] = self.items_looked.get(opening, 0) + 1
        self.scans += [dict(sc, time=when.isoformat(timespec="seconds"))
                       for sc in scans_from_map_screen(self.locator, image)]
        spots = squad_on_map_screen(self.locator, image)
        if self.mine and self.mine in spots:
            fill_from_map_screens(self.path, [(when, spots[self.mine])], "when")
        if self.tracker:
            for colour, trail in self.tracker.trails.items():
                if colour in spots:
                    fill_from_map_screens(trail, [(when, spots[colour])], "when")
        self.dirty = True
        return bool(rings)

    def flight_shot(self, image, when):
        """A full-map shot from the dropship / just after the jump shows the flight line: keep it."""
        if len(self.flights_seen) >= 3 or (self.path and (when - self.path[0]["when"]).total_seconds() > FLIGHT_SHOT_AFTER):
            return
        seen = flight_from_map_screen(self.locator, image)
        if seen:
            self.flights_seen.append(seen)
            self.dirty = True

    def replay(self, folder):
        import json
        from datetime import datetime
        self.clear()
        events = json.loads((folder / "match.json").read_text())["events"]
        for e in events:
            when = datetime.fromisoformat(e["time"])
            if e["event"] == "ring_timer":
                self.timer.add(when, folder / e["file"])
            elif e["event"] == "minimap":
                image = cv2.imread(str(folder / e["file"]))
                if image is not None:
                    self.minimap(image, when, e["screen"])
            elif e["event"] in ("squad", "full") and e["screen"] in ("in_game", "spectate") and e.get("file"):
                image = cv2.imread(str(folder / e["file"]))
                if image is not None:
                    panel, s = squad_panel_of(image) if e["event"] == "full" else \
                        (image, image.shape[1] / (SQUAD_PANEL[2] - SQUAD_PANEL[0]))
                    self.squad_panel(panel, when, s, e["screen"])
            elif e["screen"] == "map" and e.get("file") and not self.locator and len(self.early_maps) < 3:
                shot = cv2.imread(str(folder / e["file"]))
                if shot is not None:
                    self.early_maps.append((shot, when))
            elif e["screen"] == "map" and e.get("file") and self.locator:
                shot = cv2.imread(str(folder / e["file"]))
                if shot is not None:
                    self.flight_shot(shot, when)
                    from Map_Locate import rings_on_map_screen
                    self.rings += [dict(r, time=e["time"]) for r in rings_on_map_screen(self.locator, shot, self.prior)]
                    self.scans += [dict(sc, time=e["time"]) for sc in scans_from_map_screen(self.locator, shot)]
                    import Map_Icons
                    self.item_sightings += Map_Icons.map_sightings(self.locator, shot, when, Map_Icons.DRAWN) or []
        print(f"[live map] caught up on {folder.name}")
        self.draw()

    def get_locator(self, name):
        if name not in self.locators:
            from Map_Locate import MapLocator
            self.locators[name] = MapLocator(name)
        return self.locators[name]

    def identify(self, minimap):
        """Which map this is: the one the minimaps keep matching. One map is tried per minimap (last
        game's map first, then the rest in turn), so this costs no more than following you does."""
        from Map_Locate import MAP_NAMES
        names = sorted(MAP_NAMES, key=lambda n: n != self.last_map)
        name = names[(self.attempts - 1) % len(names)]
        try:
            where = self.get_locator(name).locate(minimap)
        except FileNotFoundError:
            return
        if where and where["inliers"] >= 10:
            self.tries[name] = self.tries.get(name, 0) + 1
            if self.tries[name] >= 2:
                self.locator, self.last_map = self.get_locator(name), name
                self.locators = {name: self.locator}      # the other maps' features aren't needed now
                self.base = base_map(self.locator)
                print(f"[live map] {name}")

    def minimap(self, image, when, screen):
        if not self.locator:
            # Trying every map on every minimap is the expensive part, so only for the first minute
            self.attempts += 1
            if self.attempts <= IDENTIFY_TRIES:
                self.early_minimaps.append((image, when, screen))
                self.identify(image)
                if self.attempts == IDENTIFY_TRIES and not self.locator:
                    print("[live map] couldn't recognise this map (does Maps/<map>/reference.png match the game?)")
                    self.early_minimaps, self.early_maps = [], []
            if self.locator:
                # Now it's known: place what was seen while working it out (the dropship's flight, often)
                minimaps, self.early_minimaps = self.early_minimaps, []
                for args in minimaps:
                    self.minimap(*args)
                maps, self.early_maps = self.early_maps, []
                for args in maps:
                    self.map_screen(*args)
            return
        where = self.locator.place(image, when, self.last)
        if not where:
            # Kept a while: once one is placed, these are lined up with it backwards. A dark spot can go a minute
            # without a minimap matching the map picture, and lined up forwards only they were lost (2026-11-03
            # 01:57: the teammate you watched walked off the live map for 45 s; the final read had them)
            self.unplaced = self.unplaced[-UNPLACED_KEEP:] + [(image, when, screen)]
            return
        if self.unplaced:
            self.back_fill(image, when, where)
        self.placed(image, when, screen, where)

    def back_fill(self, image, when, where):
        """The minimaps that couldn't be placed since the last one that was, lined up backwards from this one
        (each a second or two from the next), then used oldest first: the trails only ever grow at the end."""
        from Map_Locate import TRACK_GAP
        filled, after = [], (image, when, where["fit"])
        for item in reversed(self.unplaced):
            if (after[1] - item[1]).total_seconds() > TRACK_GAP:
                break
            step = self.locator.track(after[0], after[2], item[0])
            if not step:
                break
            filled.append((item, step))
            after = (item[0], item[1], step["fit"])
        self.unplaced = []
        for (old, at, screen), step in reversed(filled):
            self.placed(old, at, screen, step)

    def placed(self, image, when, screen, where, waited=False):
        from Map_Locate import MINIMAP_MARGIN, MAX_SPEED, JUMP_CONFIRM
        if not waited:
            self.last = (when, image, where["fit"], where)
            if screen == "spectate":
                # Whose squad you're watching comes from the squad panel (every 5 s): until it says, the minimap waits
                if self.other_squad is None and (not self.undecided or
                                                 (when - self.undecided[0][1]).total_seconds() < SPECTATE_DECIDE):
                    self.undecided.append((image, when, where))
                    return
                self.decided()
            elif screen in ("in_game", "dropship", "after_squad"):
                self.decided()
                self.other_squad = None          # alive again (or out): the next spectating is worked out afresh
        if screen == "spectate" and self.other_squad:
            screen = "after_squad"               # another squad's minimap: only the ring on it is ours
        if screen == "after_squad":
            # Watching another squad after yours is out: only the ring on their minimap is ours
            self.ring_sample(image, when, where["fit"])
            self.dirty = True
            return
        if screen in ("in_game", "spectate"):
            import Map_Icons
            self.item_minimaps += 1
            if self.item_minimaps % Map_Icons.MINIMAP_EVERY == 0:
                self.item_sightings += Map_Icons.minimap_sightings(image, where, MINIMAP_MARGIN, when, Map_Icons.DRAWN)
        point = {"when": when, "x": where["x"], "y": where["y"], "screen": screen}
        h = image.shape[0] // 2
        centre = colour_of(cv2.cvtColor(image[h - 10:h + 10, h - 10:h + 10], cv2.COLOR_BGR2HSV))
        if self.mine is None and screen != "spectate":     # spectating, the middle is a teammate
            if centre:
                self.votes[centre] = self.votes.get(centre, 0) + 1
                if self.votes[centre] >= 5:     # enough looks at the middle arrow: that colour is you
                    self.mine = centre
                    if self.tracker:            # already following teammates (joined while dead): keep them
                        self.tracker.colours = [c for c in self.tracker.colours if c != centre]
                        self.tracker.trails.pop(centre, None)
                    else:
                        self.tracker = TeammateTracker([c for c in SQUAD_COLOURS if c != centre], MINIMAP_MARGIN,
                                                       self.locator.reference)
        if self.tracker is None and screen == "spectate" and centre:
            # Joined while dead: nobody's colour is known yet, but whoever is in the middle is a teammate
            self.tracker = TeammateTracker(list(SQUAD_COLOURS), MINIMAP_MARGIN, self.locator.reference)
        if screen != "spectate" and self.tracker and centre in self.tracker.gone:
            centre = None                        # a quitter can't be in the middle: that's map art under your arrow
        spectating = screen == "spectate" or (self.mine is not None and centre and centre != self.mine)
        if screen == "dropship" and not self.path:       # (later dropships: redeploying in a respawn mode)
            self.ship.append(point)
        elif self.tracker and centre and centre != self.mine and spectating:
            # Dead and watching a teammate: the middle of the minimap is them
            self.tracker.add_point(centre, dict(point, watched=True))
        else:
            last = self.path[-1] if self.path else None
            far = lambda a, b: np.hypot(b["x"] - a["x"], b["y"] - a["y"]) > MAX_SPEED * max((b["when"] - a["when"]).total_seconds(), 1)
            if last and far(last, point):
                # Too far to have walked: a mismatch, or respawned elsewhere - believed once JUMP_CONFIRM agree
                if self.jumped and far(self.jumped[-1], point):
                    self.jumped = []
                self.jumped.append(point)
                if len(self.jumped) < JUMP_CONFIRM:
                    return
                self.jumped[0]["gap"] = True
                self.path.extend(self.jumped[:-1])
                self.jumped = []
            self.jumped = []
            self.path.append(point)
            if self.tracker:
                self.tracker.add(image, where["fit"], when, (where["x"], where["y"]))
        if screen != "dropship":
            self.ring_sample(image, when, where["fit"])
        self.dirty = True

    def decided(self):
        """The spectating minimaps that were waiting for the squad panel, used now: another squad's for the ring
        only (as after your squad is out), else the teammate in the middle (also when the panel never said)."""
        held, self.undecided = self.undecided, []
        for image, when, where in held:
            self.placed(image, when, "spectate", where, waited=True)

    def ring_sample(self, image, when, fit):
        """The ring on this minimap for rings_from_samples - none while the ring timer says it's closing."""
        from Map_Locate import minimap_ring, not_closing
        ring = minimap_ring(image, fit, self.locator.ring_sizes, self.prior)
        self.ring_samples.append((when, not_closing(ring, when, self.timer)))

    def squad_panel(self, panel, when, s, screen="in_game"):
        """Who's still in the squad. A teammate who quits drops off the squad panel; from then on whatever
        their colour is on the minimap is the map's own art (Olympus is full of orange), so their trail stops.
        Spectating, it also says whose squad you're watching (other_squad_panel)."""
        from Extract_Stats import is_me
        names, colours = read_squad_panel(panel, s)
        if screen == "spectate":
            teammates = [max(v, key=v.get) for v in self.panel_names.values() if max(v.values()) >= 2]
            other = other_squad_panel(names, colours, teammates)
            if other is not None and other != self.other_squad:
                self.other_squad = other
                if other:
                    print(f"[live map] spectating another squad ({', '.join(colours.values())}), not yours")
                self.decided()
            if self.other_squad:
                return                           # their names aren't your teammates'
        for colour, name in colours.items():
            if colour != self.mine and not is_me(name):
                votes = self.panel_names.setdefault(colour, {})
                votes[name] = votes.get(name, 0) + 1
        if not any(is_me(n) for n in names):
            return                               # couldn't read the panel this time: says nothing
        for colour, votes in self.panel_names.items():
            name = max(votes, key=votes.get)
            if votes[name] < 2:
                continue
            if on_panel(name, names):
                self.last_on_panel[colour], self.missing[colour] = when, 0
                if self.tracker and colour in self.tracker.gone:
                    self.tracker.gone.discard(colour)        # back after all (e.g. misread)
            else:
                self.missing[colour] = self.missing.get(colour, 0) + 1
                if self.missing[colour] >= LEFT_AFTER and self.tracker and colour not in self.tracker.gone \
                        and colour in self.last_on_panel:
                    self.tracker.quit(colour, self.last_on_panel[colour])
                    print(f"[live map] {name} ({colour}) left the match")
                    self.dirty = True

    def confirmed(self, trail):
        """No names to check live, so a teammate is a colour seen moving near you: squads drop together,
        and respawn beacons / lava never move."""
        if len(trail) < 5 or not self.path:
            return False
        me = {p["when"]: p for p in self.path}
        near = [p for p in trail if p["when"] in me and np.hypot(p["x"] - me[p["when"]]["x"], p["y"] - me[p["when"]]["y"]) <= 30]
        spread = max(np.ptp([p["x"] for p in trail]), np.ptp([p["y"] for p in trail]))
        return len(near) >= 5 and spread >= 15

    def draw(self):
        if not self.locator:
            return
        mates = {c: t for c, t in self.tracker.trails.items() if self.confirmed(t)} if self.tracker else {}
        names = {self.mine: "You"} if self.mine else {}
        names.update({c: "Teammate" for c in mates})
        from Map_Locate import rings_from_samples
        # Every sighting since the last redraw is fitted, then only the last 90 s are kept for the next (earlier rings
        # are in the list already). It was trimmed to 90 s first: a redraw put off by a backlog threw a steady run away
        # unfitted - 2026-11-06 02:00, round 4's ring (20 minimaps, 01:59:59-02:00:18) was never drawn live, while the
        # same minimaps replayed in order find it at 02:00:07
        if self.ring_samples:
            self.minimap_rings = distinct_rings(self.minimap_rings + rings_from_samples(
                self.ring_samples, self.locator.reference.shape[1], self.locator.ring_sizes))
            latest = self.ring_samples[-1][0]
            self.ring_samples = [x for x in self.ring_samples if (latest - x[0]).total_seconds() <= 90]
        rings = consistent_rings(self.rings + self.minimap_rings, self.locator.reference.shape[1::-1], self.locator.ring_sizes)
        from Map_Locate import ring_prior
        self.prior = ring_prior(rings)           # where the next minimap / map shot looks for the ring
        rings = set_size_rings(rings, self.locator)
        self.write_rings(rings)
        # Where you landed, so the skydive is drawn as a dashed flight line, like the finished picture
        from Map_Locate import find_landing
        landing = None
        if self.path:
            start = self.path[0]["when"]
            timed = [dict(p, t=(p["when"] - start).total_seconds(), screen="in_game") for p in self.path]
            # Not landed yet: it's all still skydive
            landing = find_landing(timed, 0) or self.path[-1]
        jump = dict(self.path[0], time=self.path[0]["when"].isoformat(timespec="seconds")) if self.path else None
        flight = best_flight(self.locator.reference.shape[1::-1], ship=self.ship, jump=jump, seen=self.flights_seen)
        import Map_Icons
        render_both(self.locator, LATEST_MAP, LATEST_ZOOM, self.path, mates=mates, ship=flight,
                    landing=landing, scans=distinct_scans(self.scans), items=Map_Icons.group_sightings(self.item_sightings),
                    rings=rings, header=f"LIVE  \u00b7  {self.locator.map_name}", my_colour_name=self.mine,
                    names=names, base=self.base)
        self.dirty, self.drawn = False, time.monotonic()


def set_size_rings(rings, locator):
    """The live map draws each ring at its round's set size (MapLocator.ring_size: measured ones wobble), and leaves
    out a ring that's no round's size - caught closing, or the last ring closing to nothing. A map whose scale isn't
    known yet can't tell those apart: a ring of a size it has learned (within 12%) is drawn at that size, the rest as
    measured (not ring_size: with only round 2 learned, that would take its 75%-small last-round slack)."""
    if not locator.px_per_m:
        learned = lambda r: next((s for s in locator.ring_sizes if abs(r - s) <= max(0.12 * s, 4)), r)
        return [dict(ring, r=learned(ring["r"])) for ring in rings]
    out = []
    for ring in rings:
        size = locator.ring_size(ring["r"])
        if size:
            out.append(dict(ring, r=size))
    return out


def redraw_from_trails(folder):
    """Draw a match's picture again from its trails.json alone (for matches whose screenshots are gone,
    after changes to how pictures are drawn). Writes match_map.jpg."""
    import json
    from datetime import datetime
    from Map_Locate import MapLocator
    from Extract_Stats import RESPAWN_MODES
    folder = Path(folder)
    data = json.loads((folder / "trails.json").read_text())
    respawns = data.get("mode") in RESPAWN_MODES
    locator = MapLocator(data["map"])
    pts = lambda player: [dict(q, when=datetime.fromisoformat(q["time"])) for q in player["points"]]
    me = next((p for p in data["players"] if p["is_me"]), None)
    mates = {p["colour"]: pts(p) for p in data["players"] if not p["is_me"] and p["colour"]}
    names = {p["colour"]: p["name"] + (" (you)" if p["is_me"] else "") for p in data["players"] if p["colour"]}
    ship = data.get("dropship") if isinstance(data.get("dropship"), dict) else None
    tmp = folder / ".redraw.png"
    render_both(locator, tmp, folder / ".redraw_zoom.png", pts(me) if me else [], mates=mates, landing=data.get("landing"),
                death=data.get("death"), downs=data.get("downs", []), ship=ship, rings=data.get("rings", []),
                scans=data.get("scans", []), items=data.get("items", []),
                header=data.get("title", "").replace(" \u00b7 ", "  \u00b7  "), my_colour_name=me["colour"] if me else None,
                names=names, respawns=respawns)
    cv2.imwrite(str(folder / "match_map.jpg"), cv2.imread(str(tmp)), [cv2.IMWRITE_JPEG_QUALITY, 88])
    write_svg(locator, folder / "match_paths.svg", data["players"], rings=data.get("rings", []), ship=ship,
              landing=data.get("landing"), death=data.get("death"), downs=data.get("downs", []),
              scans=data.get("scans", []), items=data.get("items", []), title=data.get("title", ""), map_href=data.get("reference"),
              respawns=respawns)
    tmp.unlink()
    (folder / ".redraw_zoom.png").unlink(missing_ok=True)
