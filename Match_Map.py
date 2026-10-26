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
ZOOM_PADDING = 0.15                      # zoomed view: room around the trails (share of their extent)
ZOOM_MIN_SIZE = 0.3                      # zoomed view: never closer than this share of the map (any closer
                                         # is just blur: the map only has the minimap's own detail)
SMOOTH_WINDOW = 5                        # teammate trails: rolling median over this many sightings
MAP_VIEW_BOX = (455, 0, 1466, 1014)       # where the map is drawn on the full-map screen (1920x1080): past it
                                         # is the game and the button bar, which look like an untinted ring
OUTPUT_SIZE = 1600                       # px, longest side of the match picture
LIVE_EVERY = 5                           # s between live redraws of "Latest Match Map.png"
IDENTIFY_TRIES = 60                      # minimaps to spend working out the map before giving up on this match

# Squad colours: hue range (OpenCV 0-180) to find them, and the colour to draw them in (BGR)
SQUAD_COLOURS = {
    "cyan": {"hue": (80, 100), "draw": (255, 220, 40)},
    "green": {"hue": (35, 70), "draw": (70, 235, 110)},
    "orange": {"hue": (5, 25), "draw": (40, 150, 255)},
}
ARROW_AREA = (5, 90)                     # px area of a squad arrow on the minimap (pings are bigger)
ARROW_BOX = 16                           # px; arrows fit in this box, map icons don't
TEAMMATE_SPEED = 0.02                    # map widths per second a teammate can cover (zips, redeploys)
CONFIRM_SIGHTINGS = 4                    # a teammate's trail (re)starts only after this many steady sightings
SCAN_RADIUS = (0.07, 0.17)               # survey beacon scan radius, as a share of the map picture's width
SCAN_MIN_FIT = 0.35                      # share of the violet outline that must sit on the circle
SCAN_MIN_FILL = 0.35                     # share of the inside that must be violet
ARROW_MIN_VALUE = 100                    # brightness an arrow must reach (0-255)
ICON_RADIUS = 7                          # px on the reference: a same-coloured map icon this close = not a teammate
STAGE_RATIO = 0.4                        # each ring is at least this share of the one before (else a stage is missing)
TRAIL_GAP = 8                            # s; unseen longer than this = don't draw a line across the gap
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

def squad_colours(dropship_image):
    """{colour: player name} from the squad panel at the bottom left (each name has a coloured frame)."""
    from Extract_Stats import read_text
    if dropship_image is None:
        return {}
    s = dropship_image.shape[1] / 1920
    top = int(760 * s)
    panel = dropship_image[top:, : int(460 * s)]     # just the squad panel, not the rest of the HUD
    hsv = cv2.cvtColor(dropship_image, cv2.COLOR_BGR2HSV)
    out = {}
    for x, y, text in read_text(panel):
        name = re.sub(r"^\[[^\]]*\]\s*", "", text).strip()
        if len(name) < 3 or re.search(r"NEXT|EVO|LVL", text.upper()):
            continue
        y += top
        # The coloured frame runs down the left of each row
        colour = colour_of(hsv[max(0, y - int(30 * s)): y + int(50 * s), int(40 * s): int(115 * s)])
        if colour and colour not in out:
            out[colour] = name
    return out

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
    n, _, stats, centres = cv2.connectedComponentsWithStats(mask)
    h, w = mask.shape
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        cx, cy = centres[i]
        if not ARROW_AREA[0] <= area <= ARROW_AREA[1] or bw > ARROW_BOX or bh > ARROW_BOX:
            continue
        if area < 0.3 * bw * bh:                  # arrows are solid; icon outlines aren't
            continue
        if not margin <= cx <= w - margin or not margin <= cy <= h - margin:
            continue
        out.append((float(cx), float(cy)))
    return out

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
        trail = self.trails[colour]
        if trail and not steady(trail[-1], point, self.speed):
            point["gap"] = True
        trail.append(point)

    def add(self, minimap, fit, when, me):
        for colour in self.colours:
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

def tinted_inside(view, cx, cy, r):
    """True if a disc is coloured inside (a blue dome shield and the like), not the ring's grey wash:
    the next ring lightens the map without changing its colour."""
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
    blue = ((hsv[..., 0] >= 90) & (hsv[..., 0] <= 130) & (hsv[..., 1] > 80))[inside > 0].mean()
    return blue > 0.35 or sat_in - sat_out > 40

def brighter_disc(view, min_share=0.75):
    """The next ring drawn as a lighter disc (Worlds Edge, Broken Moon early on): the circle with the most
    consistent "brighter inside than outside" edge. (cx, cy, r, share) in view px, or None. A lower
    min_share finds faint discs too (used when cleaning the map, where a miss costs more than a false hit)."""
    gray = cv2.GaussianBlur(cv2.cvtColor(view, cv2.COLOR_BGR2GRAY), (0, 0), 3).astype(np.float32)
    small = cv2.resize(gray, None, fx=0.5, fy=0.5)
    circles = cv2.HoughCircles(small.astype(np.uint8), cv2.HOUGH_GRADIENT, dp=1.5, minDist=20,
                               param1=40, param2=25, minRadius=25, maxRadius=int(max(small.shape)))
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

def ring_from_hole(view):
    """The ring as the untinted area inside the red "outside the ring" tint: the biggest untinted patch
    whose outline is a circle, tinted just outside it and clear just inside. Works however the tint looks
    over the background (red over land, pink over Olympus' sky). (cx, cy, r, support) in view px, or None."""
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
            if support >= 0.6 and (best is None or circle[2] > best[2]) and tint_steps_at(red, circle):
                best = (circle[0], circle[1], circle[2], support)
    return best

def tint_steps_at(red, circle):
    """Is the tint really on the outside of this circle: mostly tinted just outside its on-screen edge,
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
    return outside >= 0.75 and inside <= 0.4

def violet_mask(image):
    """The violet of a survey beacon scan (dark fill, brighter rim)."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return ((hsv[..., 0] >= 118) & (hsv[..., 0] <= 150) & (hsv[..., 1] > 90) & (hsv[..., 2] > 15)).astype(np.uint8)

def scans_from_map_screen(locator, screenshot):
    """Survey beacon scans (violet discs) on a full-map screenshot, on the reference: [{"x", "y", "r"}].
    Violet that's part of the map itself (it's in the map picture too) doesn't count."""
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    where = locator.locate_image(view)
    if not where:
        return []
    fit, scale = where["fit"], where["scale"]
    h, w = view.shape[:2]
    on_map = cv2.warpAffine(locator.reference, cv2.invertAffineTransform(fit), (w, h))
    plain = violet_mask(view)
    mask = plain & (1 - cv2.dilate(violet_mask(on_map), np.ones((9, 9), np.uint8)))
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
        if not (SCAN_RADIUS[0] * W <= r <= SCAN_RADIUS[1] * W and 0 <= cx <= W and 0 <= cy <= H):
            continue
        inside = np.zeros((h, w), np.uint8)
        cv2.circle(inside, (int(circle[0]), int(circle[1])), int(circle[2] * 0.9), 1, -1)
        if inside.sum() == 0 or plain[inside > 0].mean() < SCAN_MIN_FILL:
            continue                              # a real scan is violet all the way through
        out.append({"x": float(cx), "y": float(cy), "r": float(r)})
    return out

MAP_ARROW_AREA = (60, 450)               # px area of a squad arrow on the full map (bigger than on the minimap)
MAP_ARROW_BOX = 40

def squad_on_map_screen(locator, screenshot):
    """Squad arrows on a full-map screenshot: {colour: [(x, y), ...]} on the reference. While the map is
    open there's no minimap, so this is what keeps everyone's trail going."""
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    where = locator.locate_image(view)
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
    player last was (if it's a believable distance away). `sightings`: [(datetime, [(x, y), ...])]."""
    from datetime import datetime
    when = (lambda p: p[time_key] if isinstance(p[time_key], datetime) else datetime.fromisoformat(p[time_key]))
    added = []
    for t, spots in sightings:
        before = [p for p in trail + added if when(p) <= t]
        if not before or not spots:
            continue
        last = max(before, key=when)
        if (t - when(last)).total_seconds() > max_gap:
            continue
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

def rings_from_map_screen(locator, screenshot):
    """Ring circles (on the reference) seen on one full-map screenshot."""
    s = screenshot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in MAP_VIEW_BOX)
    view = screenshot[y1:y2, x1:x2]
    where = locator.locate_image(view)
    if not where:
        return []
    fit, scale = where["fit"], where["scale"]
    out = []
    disc = brighter_disc(view)
    if disc:
        cx, cy = fit @ np.array([disc[0], disc[1], 1.0])
        out.append({"kind": "next ring", "x": float(cx), "y": float(cy), "r": float(disc[2] * scale)})
    found_kinds = {"next ring"} if disc else set()
    hole = ring_from_hole(view)
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
    for kind, circle in partial_rings(view, scale, locator.info.get("ring_sizes", [])):
        if kind in found_kinds:
            continue
        cx, cy = fit @ np.array([circle[0], circle[1], 1.0])
        out.append({"kind": kind, "x": float(cx), "y": float(cy), "r": float(circle[2] * scale), "partial": True})
        found_kinds.add(kind)
    ring = next((r for r in out if r["kind"] == "ring"), None)
    if ring:
        out = [r for r in out if r["kind"] == "ring" or
               (r["r"] < ring["r"] and np.hypot(r["x"] - ring["x"], r["y"] - ring["y"]) + r["r"] <= ring["r"] * 1.05)]
    return out

# ---------------------------
# DRAWING
# ---------------------------

def dashed_line(image, a, b, colour, width, dash=10):
    a, b = np.array(a, float), np.array(b, float)
    length = np.linalg.norm(b - a)
    for start in np.arange(0, length, 2 * dash):
        p = a + (b - a) * start / max(length, 1)
        q = a + (b - a) * min(start + dash, length) / max(length, 1)
        cv2.line(image, tuple(int(v) for v in p), tuple(int(v) for v in q), colour, width, cv2.LINE_AA)

def ring_stage_check(ring, sizes):
    """A ring that isn't one of this map's known ring sizes was caught mid-close: bigger than the first ring
    (the largest size: it's seen from the dropship every game), or between two sizes that are one stage
    apart. It becomes a "closing ring" (a next ring never closes, so that one is dropped: None). Inside a
    wider gap (the last rings, too rarely seen to be learned) a size is left alone."""
    if len(sizes) < 2 or ring["kind"] == "closing ring" or any(abs(ring["r"] - s) <= 0.06 * s for s in sizes):
        return ring
    bigger = [s for s in sizes if s > ring["r"]]
    smaller = [s for s in sizes if s < ring["r"]]
    if bigger and (not smaller or max(smaller) < STAGE_RATIO * min(bigger)):
        return ring
    return dict(ring, kind="closing ring") if ring["kind"] == "ring" else None

def consistent_rings(rings, size=None, sizes=()):
    """Rings only ever shrink, and each next ring sits inside the ring / next ring before it: drop
    sightings that break that (a survey beacon's scan, a dome, a bad fit). `size`: (w, h) of the map
    picture - a ring has to be centred on the map. `sizes`: this map's known ring sizes (ring_stage_check)."""
    out = []
    rings = [r for r in (ring_stage_check(r, sizes) for r in rings) if r]
    for ring in sorted(rings, key=lambda r: r.get("time", "")):
        if size and not (-0.05 * size[0] <= ring["x"] <= 1.05 * size[0] and -0.05 * size[1] <= ring["y"] <= 1.05 * size[1]):
            continue
        # A next ring off the minimap needs a ring around it (a lone light disc early on is a dome / scan)
        if ring["kind"] == "next ring" and ring.get("source") == "minimap" and not any(c["kind"] == "ring" for c in out):
            continue
        # Rings never grow back: bigger than a ring already seen means it isn't one
        if any(c["kind"] == "ring" and c["r"] < ring["r"] * 0.95 for c in out):
            continue
        # The latest accepted circle at least as big as this one (a closing ring is on the move: skipped)
        container = next((c for c in reversed(out) if c["kind"] != "closing ring" and c["r"] >= ring["r"] * 0.95), None)
        if container:
            slack = container["r"] * 0.1 + 5      # measured centres wobble a few px
            if np.hypot(ring["x"] - container["x"], ring["y"] - container["y"]) + ring["r"] > container["r"] + slack:
                continue
        out.append(ring)
    return out

def flight_line(size, heading=None, ship=(), jump=None):
    """The dropship's whole flight across the map: a line through where it was seen (dropship minimaps,
    else where you jumped out of it) in the direction it flew (the compass heading on the dropship, else the
    way the dropship minimaps moved), stretched edge to edge of the map picture. None if not enough to go on.
    Returns {"start", "end", "heading", "jump"} with points as {"x", "y"} on the map picture."""
    pts = np.array([[p["x"], p["y"]] for p in ship], float) if ship else np.empty((0, 2))
    if heading is None and len(pts) >= 2 and np.hypot(*(pts[-1] - pts[0])) >= 15:
        dx, dy = pts[-1] - pts[0]
        heading = float(np.degrees(np.arctan2(dx, -dy)) % 360)
    if heading is None:
        return None
    if len(pts):
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
    if jump:
        # Where you left it: your first spot after jumping, put back onto the line
        j = np.array([jump["x"], jump["y"]], float)
        on = anchor + np.dot(j - anchor, d) * d
        out["jump"] = {"x": float(on[0]), "y": float(on[1]), "time": jump.get("time", "")}
    return out

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
    for t in np.arange(60, length - 30, 160):
        tip = a + d * t
        for side in (1, -1):
            tail = tip - d * size + n * size * 0.7 * side
            cv2.line(image, tuple(tip.astype(int)), tuple(tail.astype(int)), colour, 3 if zoomed else 2, cv2.LINE_AA)
    if flight.get("jump"):
        j = P(flight["jump"])
        cv2.circle(image, j, 9, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(image, j, 3, colour, -1, cv2.LINE_AA)

def ring_label(ring):
    name = {"ring": "Ring", "next ring": "Next ring", "closing ring": "Closing"}[ring["kind"]]
    if ring["kind"] == "ring" and ring.get("round"):
        name = f"Ring {ring['round']}"
    return f"{name} {ring.get('time', '')[11:16]}".strip()

def distinct_rings(rings):
    """The same ring seen several times (or as "next ring", then as the ring) is drawn once, as the
    latest sighting."""
    out = []
    for ring in sorted(rings, key=lambda r: r.get("time", "")):
        for i, seen in enumerate(out):
            if (abs(seen["r"] - ring["r"]) <= 0.05 * ring["r"] + 3
                    and np.hypot(seen["x"] - ring["x"], seen["y"] - ring["y"]) <= 0.05 * ring["r"] + 3):
                out[i] = dict(ring, kind="ring" if "ring" in (seen["kind"], ring["kind"]) and ring["kind"] == "ring" else ring["kind"])
                break
        else:
            out.append(ring)
    return out

def font(size):
    from PIL import ImageFont
    for path in FONT_FILES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def base_map(locator, region=None):
    """The reference (or the `region` x1, y1, x2, y2 of it), with the stitched minimaps laid over it
    wherever they've been seen. Returns (image, zoom, seen, offset): picture px = (ref - offset) * zoom."""
    ref = locator.reference
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
        # Survey scans got stitched in from some minimaps: show the plain map picture there instead
        scan_tint = cv2.dilate(violet_mask(sharp.astype(np.uint8)), np.ones((7, 7), np.uint8))
        seen[scan_tint > 0] = 0
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

def render(locator, out_file, me_trail, mates=None, ship=None, rings=(), landing=None, death=None,
           header="", my_colour_name=None, names=None, base=None, region=None, downs=(), scans=()):
    """Draw the match picture. Trails are lists of {"x", "y"} on the reference. `base`: a base_map()
    result to reuse (the live map redraws often). `region`: draw just that part of the map (zoomed)."""
    from PIL import Image, ImageDraw
    image, zoom, seen, (ox, oy) = base if base else base_map(locator, region)
    image = image.copy()
    zoomed = region is not None
    P = lambda p: (int((p["x"] - ox) * zoom), int((p["y"] - oy) * zoom))
    rings = distinct_rings(rings)
    for ring in rings:
        # Ring: solid white; next ring: thinner, light grey; caught mid-close: dashed orange
        if ring["kind"] == "closing ring":
            for start in range(0, 360, 12):
                cv2.ellipse(image, P(ring), (int(ring["r"] * zoom),) * 2, 0, start, start + 6, (80, 170, 255), 2, cv2.LINE_AA)
            continue
        colour, width = ((255, 255, 255), 3) if ring["kind"] == "ring" else ((190, 190, 190), 2)
        cv2.circle(image, P(ring), int(ring["r"] * zoom), colour, width, cv2.LINE_AA)
    for scan in scans:
        # Survey beacon scan: faint dotted circle, pin in the middle
        r = int(scan["r"] * zoom)
        for start in range(0, 360, 6):
            cv2.ellipse(image, P(scan), (r, r), 0, start, start + 2, (230, 120, 170), 1, cv2.LINE_AA)
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
        for a, b in zip(trail, trail[1:]):
            if landed and point_time(b) <= landed:
                # Skydiving from the dropship: not on the ground yet, so a thin dashed flight line
                dashed_line(image, P(a), P(b), draw, 2)
                continue
            # No line across a gap (dead, respawned somewhere else, or just not seen)
            if b.get("gap") or (point_time(b) - point_time(a)).total_seconds() > TRAIL_GAP:
                continue
            # The teammate you were watching while dead: as bold as your own trail
            bold = a.get("watched") and b.get("watched")
            cv2.line(image, P(a), P(b), draw, (6 if zoomed else 5) if bold else width, cv2.LINE_AA)
        if trail and colour != (my_colour_name or "me"):
            cv2.drawMarker(image, P(trail[-1]), draw, cv2.MARKER_TILTED_CROSS, 18, 3)
    P = plain_P                                   # markers below sit exactly where things happened
    for scan in scans:
        # Pin in the middle of each survey beacon scan, on top of the trails
        cx, cy = P(scan)
        size = 1.4 if zoomed else 1.0
        tip, head = (cx, cy), (cx, int(cy - 22 * size))
        cv2.line(image, tip, head, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.circle(image, head, int(9 * size), (230, 110, 180), -1, cv2.LINE_AA)
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
            draw.text((x, y - int(ring["r"] * zoom) - 14), ring_label(ring), font=small, anchor="mm",
                      fill={"ring": (255, 255, 255), "closing ring": (255, 170, 80)}.get(ring["kind"], (200, 200, 200)),
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

def write_svg(locator, out_file, players, rings=(), ship=None, landing=None, death=None, title="", map_href=None, downs=(), scans=()):
    landed_at = landing.get("time") if landing else None
    """The match as vector layers on the map picture's own coordinates, to zoom in as far as you like or
    lay over the map in another program. `players`: [{"name", "colour", "is_me", "points": [{"time", "x", "y"}]}].
    Layers (SVG groups): map, rings, dropship, players (one group each), events, pois."""
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
        colour = {"ring": "#ffffff", "closing ring": "#ffaa50"}.get(ring["kind"], "#bebebe")
        dash = ' stroke-dasharray="6 6"' if ring["kind"] == "closing ring" else ""
        out.append(f'<circle class="line" cx="{ring["x"]:.1f}" cy="{ring["y"]:.1f}" r="{ring["r"]:.1f}" stroke="{colour}" '
                   f'stroke-width="2"{dash} data-kind="{ring["kind"]}" data-time="{ring.get("time", "")}" '
                   f'data-round="{ring.get("round") or ""}" data-source="{ring.get("source", "map screen")}">'
                   f'<title>{escape(ring_label(ring))}</title></circle>')
    out.append("</g>")
    out.append('<g id="scans">')
    for scan in scans:
        out.append(f'<circle class="line" cx="{scan["x"]:.1f}" cy="{scan["y"]:.1f}" r="{scan["r"]:.1f}" stroke="#aa78e6" '
                   f'stroke-width="1" stroke-dasharray="2 6" opacity="0.7"><title>Survey scan {scan.get("time", "")[11:]}</title></circle>')
        out.append(f'<circle cx="{scan["x"]:.1f}" cy="{scan["y"]:.1f}" r="3" fill="#aa78e6" stroke="#ffffff" stroke-width="1">'
                   f'<title>Survey scan {scan.get("time", "")[11:]}</title></circle>')
    out.append("</g>")
    if ship:
        a, b = ship["start"], ship["end"]
        out.append('<defs><marker id="ship-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="5" '
                   'markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10" fill="none" '
                   'stroke="#78c8fa" stroke-width="2"/></marker></defs>')
        out.append(f'<g id="dropship" data-heading="{ship["heading"]}"><title>Dropship {ship["heading"]}\u00b0</title>'
                   f'<line class="line" x1="{a["x"]:.1f}" y1="{a["y"]:.1f}" x2="{b["x"]:.1f}" y2="{b["y"]:.1f}" '
                   f'stroke="#78c8fa" stroke-width="2" stroke-dasharray="8 6" marker-mid="url(#ship-arrow)" '
                   f'marker-end="url(#ship-arrow)"/>')
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
        segment = []
        for i, p in enumerate(pts + [None]):
            gap = p is not None and segment and (p.get("gap") or (datetime.fromisoformat(p["time"]) - datetime.fromisoformat(segment[-1]["time"])).total_seconds() > TRAIL_GAP
                                                 or (landed_at and segment[-1]["time"] <= landed_at < p["time"]))
            if p is None or gap:
                if len(segment) > 1:
                    coords = " ".join(f'{q["x"]:.1f},{q["y"]:.1f}' for q in segment)
                    times = ",".join(q["time"][11:19] for q in segment)
                    watched = all(q.get("watched") for q in segment)
                    flight = landed_at and segment[-1]["time"] <= landed_at
                    style = ' stroke-dasharray="6 6"' if flight else ""
                    out.append(f'<polyline class="line{" watched" if watched else ""}{" flight" if flight else ""}" '
                               f'points="{coords}" stroke="{colour}" stroke-width="{1 if flight else 3 if player["is_me"] or watched else 2}"'
                               f'{style} data-times="{times}"/>')
                segment = []
            if p is not None:
                segment.append(p)
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
    """Write then rename, so OBS never reads a half-written file."""
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.tmp.png")
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
        self.locators = {}                       # map name -> MapLocator (kept: loading one takes seconds)
        self.last_map = None
        self.clear()
        threading.Thread(target=self.run, daemon=True, name="live-map").start()

    def clear(self):
        self.locator = None
        self.tries = {}                          # map name -> good matches so far, while working out the map
        self.attempts = 0
        self.path, self.ship, self.rings, self.scans = [], [], [], []
        self.votes, self.mine, self.tracker = {}, None, None
        self.last = None                         # (when, minimap, fit) of the last placed minimap
        self.ring_samples = []                   # (when, ring edge points) from the minimaps
        self.minimap_rings = []                  # rings found from them so far
        self.base, self.dirty, self.drawn = None, False, 0.0

    # ---- called from the capture loop (never blocks) ----
    def new_match(self):
        self._put(("new",))

    def catch_up(self, folder):
        """Restarted mid-match: replay what's been saved for it so far, so the picture carries on."""
        self._put(("catch up", Path(folder)))

    def add_minimap(self, image, when, screen):
        self._put(("minimap", image.copy(), when, screen))

    def add_map_screen(self, image, when):
        self._put(("map", image.copy(), when))

    def _put(self, job):
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            pass                                 # falling behind: skip a frame rather than stall capture

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
                elif job[0] == "map" and self.locator:
                    self.rings += [dict(r, time=job[2].isoformat(timespec="seconds"))
                                   for r in rings_from_map_screen(self.locator, job[1])]
                    self.scans += [dict(sc, time=job[2].isoformat(timespec="seconds"))
                                   for sc in scans_from_map_screen(self.locator, job[1])]
                    spots = squad_on_map_screen(self.locator, job[1])
                    if self.mine and self.mine in spots:
                        fill_from_map_screens(self.path, [(job[2], spots[self.mine])], "when")
                    if self.tracker:
                        for colour, trail in self.tracker.trails.items():
                            if colour in spots:
                                fill_from_map_screens(trail, [(job[2], spots[colour])], "when")
                    self.dirty = True
                if self.dirty and time.monotonic() - self.drawn >= LIVE_EVERY and self.jobs.qsize() < 5:
                    self.draw()
            except Exception:
                traceback.print_exc()

    def replay(self, folder):
        import json
        from datetime import datetime
        self.clear()
        events = json.loads((folder / "match.json").read_text())["events"]
        for e in events:
            when = datetime.fromisoformat(e["time"])
            if e["event"] == "minimap":
                image = cv2.imread(str(folder / e["file"]))
                if image is not None:
                    self.minimap(image, when, e["screen"])
            elif e["screen"] == "map" and e.get("file") and self.locator:
                shot = cv2.imread(str(folder / e["file"]))
                if shot is not None:
                    self.rings += [dict(r, time=e["time"]) for r in rings_from_map_screen(self.locator, shot)]
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
        from Map_Locate import MINIMAP_MARGIN, MAX_SPEED, TRACK_GAP
        if not self.locator:
            # Trying every map on every minimap is the expensive part, so only for the first minute
            self.attempts += 1
            if self.attempts <= IDENTIFY_TRIES:
                self.identify(image)
                if self.attempts == IDENTIFY_TRIES and not self.locator:
                    print("[live map] couldn't recognise this map (does Maps/<map>/reference.png match the game?)")
            return
        where = self.locator.locate(image)
        if not where and self.last and (when - self.last[0]).total_seconds() <= TRACK_GAP:
            where = self.locator.track(self.last[1], self.last[2], image)    # line it up with the last one
        if not where:
            return
        self.last = (when, image, where["fit"])
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
        spectating = screen == "spectate" or (self.mine is not None and centre and centre != self.mine)
        if screen == "dropship":
            self.ship.append(point)
        elif self.tracker and centre and centre != self.mine and spectating:
            # Dead and watching a teammate: the middle of the minimap is them
            self.tracker.add_point(centre, dict(point, watched=True))
        else:
            last = self.path[-1] if self.path else None
            if last and np.hypot(point["x"] - last["x"], point["y"] - last["y"]) > MAX_SPEED * max((when - last["when"]).total_seconds(), 1):
                return
            self.path.append(point)
            if self.tracker:
                self.tracker.add(image, where["fit"], when, (where["x"], where["y"]))
        if screen != "dropship":
            from Map_Locate import minimap_ring
            self.ring_samples.append((when, minimap_ring(image, where["fit"], self.locator.info.get("ring_sizes", []))))
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
        # Only the last 90 s need fitting again; earlier rings are already in the list
        if self.ring_samples:
            latest = self.ring_samples[-1][0]
            recent = [x for x in self.ring_samples if (latest - x[0]).total_seconds() <= 90]
            self.ring_samples = recent
            self.minimap_rings = distinct_rings(self.minimap_rings + rings_from_samples(
                recent, self.locator.reference.shape[1], self.locator.info.get("ring_sizes", [])))
        rings = self.rings + self.minimap_rings
        # Where you landed, so the skydive is drawn as a dashed flight line, like the finished picture
        from Map_Locate import find_landing
        landing = None
        if self.path:
            start = self.path[0]["when"]
            timed = [dict(p, t=(p["when"] - start).total_seconds(), screen="in_game") for p in self.path]
            # Not landed yet: it's all still skydive
            landing = find_landing(timed, 0) or self.path[-1]
        jump = dict(self.path[0], time=self.path[0]["when"].isoformat(timespec="seconds")) if self.path else None
        flight = flight_line(self.locator.reference.shape[1::-1], ship=self.ship, jump=jump)
        render_both(self.locator, LATEST_MAP, LATEST_ZOOM, self.path, mates=mates, ship=flight,
                    landing=landing, scans=distinct_scans(self.scans),
                    rings=consistent_rings(rings, self.locator.reference.shape[1::-1], self.locator.info.get("ring_sizes", [])), header=f"LIVE  \u00b7  {self.locator.map_name}", my_colour_name=self.mine,
                    names=names, base=self.base)
        self.dirty, self.drawn = False, time.monotonic()


def redraw_from_trails(folder):
    """Draw a match's picture again from its trails.json alone (for matches whose screenshots are gone,
    after changes to how pictures are drawn). Writes match_map.jpg."""
    import json
    from datetime import datetime
    from Map_Locate import MapLocator
    folder = Path(folder)
    data = json.loads((folder / "trails.json").read_text())
    locator = MapLocator(data["map"])
    pts = lambda player: [dict(q, when=datetime.fromisoformat(q["time"])) for q in player["points"]]
    me = next((p for p in data["players"] if p["is_me"]), None)
    mates = {p["colour"]: pts(p) for p in data["players"] if not p["is_me"] and p["colour"]}
    names = {p["colour"]: p["name"] + (" (you)" if p["is_me"] else "") for p in data["players"] if p["colour"]}
    ship = data.get("dropship") if isinstance(data.get("dropship"), dict) else None
    tmp = folder / ".redraw.png"
    render_both(locator, tmp, folder / ".redraw_zoom.png", pts(me) if me else [], mates=mates, landing=data.get("landing"),
                death=data.get("death"), downs=data.get("downs", []), ship=ship, rings=data.get("rings", []),
                scans=data.get("scans", []),
                header=data.get("title", "").replace(" \u00b7 ", "  \u00b7  "), my_colour_name=me["colour"] if me else None,
                names=names)
    cv2.imwrite(str(folder / "match_map.jpg"), cv2.imread(str(tmp)), [cv2.IMWRITE_JPEG_QUALITY, 88])
    write_svg(locator, folder / "match_paths.svg", data["players"], rings=data.get("rings", []), ship=ship,
              landing=data.get("landing"), death=data.get("death"), downs=data.get("downs", []),
              scans=data.get("scans", []), title=data.get("title", ""), map_href=data.get("reference"))
    tmp.unlink()
    (folder / ".redraw_zoom.png").unlink(missing_ok=True)
