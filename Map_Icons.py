"""
Map icons: Care Packages, Evo Harvesters, Replicators, Respawn Beacons, Ring Consoles (and a few more).
Where they were in a match (the "items" layer of match_paths.svg / trails.json), and taken out of the map
pictures' background, so the match pictures show the map and your squad, not a field of icons.

Templates: Screen_References/map_icons/<kind>.png, cut from a 1080p full-map screenshot (in game, map open);
<kind>__<variant>.png is another look of the same kind (S31's Care Package marker, white or red).
The same icons are ~0.75x that size on the minimap and ~1.1x on Maps/<Map>/stitched.png. The zoomed-out map
the reference is made from draws its diamonds differently: map_icons/far/<kind>.png, cut from reference.png.

Usage:
    .venv/bin/python Map_Icons.py "Olympus"        # rebuild that map's clean background pictures now
"""
import sys
from pathlib import Path

import cv2
import numpy as np

# ---------------------------
# CONFIGURATION
# ---------------------------

ICONS_FOLDER = Path(__file__).parent / "Screen_References" / "map_icons"
# kind -> (label, SVG colour); kinds not listed here (arsenal, lightning, white_diamond, care_package_s30) are only
# cleaned away. S31: the Care Package's location is a white diamond with the robot (care_package__marker; also red or
# yellow edged), and the Stronghold a pink-red octagon (both cut from 2026-11-05 02:45:58's E-District map). S30's grey
# robot (care_package_s30) found 6-21 "Care Packages" a game in tight clusters: not real, no longer an item
ITEMS_VERSION = 2              # trails.json "items_version": items found with these templates (Heatmap only uses those)
ITEMS = {"care_package": ("Care Package", "#ffffff"),
         "stronghold": ("Stronghold", "#ff2d6e"),
         "evo_harvester": ("Evo Harvester", "#e6f03c"),
         "replicator": ("Replicator", "#3c8cff"),
         "respawn_beacon": ("Respawn Beacon", "#50e650"),
         "ring_console": ("Ring Console", "#b4b4b4")}
DRAWN = ("care_package", "stronghold", "evo_harvester")   # also drawn on the match pictures and the live map, and
# each gets an all-games heatmap (the rest: SVG only). The Evo Harvester's S30 template still finds S31's on the minimap
# (0.73-0.91; the full map doesn't draw them); the white Care Package marker scores 0.72-0.75 on it too, but the Care
# Package's own template, searched with it, wins that spot
SCALES_MINIMAP = (0.7, 0.75, 0.8, 0.85)
SCALES_FULL_MAP = (0.55, 0.65, 0.75, 0.85, 1.0, 1.1)   # the map opened zoomed out (dropship) to zoomed in
SCALES_STITCHED = (0.9, 1.0, 1.1, 1.2, 1.3)
SCALES_REFERENCE = (0.9, 1.0, 1.1)       # the dropship map: only the big diamonds (Ring Consoles...) are drawn
ITEM_SCORE = 0.72              # template match an icon needs to count as an item
CLEAN_SCORE = 0.55             # ...to be taken out of the background (a wrong hit only hides a few px of detail)
REFERENCE_SCORE = 0.65         # (the reference is less sharp: lower than this, building squares match)
DOT_AREA = (3, 60)             # px: the small coloured dots on the dropship map (lime, blue)
POI_CHAR = (12, 22)            # px (w per letter, h) of a POI name printed on the reference
SAME_ITEM = 8                  # px on the reference: sightings this close are the same item
MIN_SIGHTINGS = 2              # an item must be seen this many times (minimaps / map shots) to be kept
MINIMAP_EVERY = 3              # look at every 3rd minimap (items don't move; ~1 a second are saved)
# A Care Package's landing spot nobody has pinged is a pale orange pulsing circle on the full map (not the minimap): an
# outline ~11-16 px across at 1080p with a smaller circle growing inside it (2026-11-05 03:05:26-03:05:59, E-District:
# two, seen on 5 shots each; 0 hits on ~70 other map shots of Storm Point / E-District). The outline is only half there
# at times (it pulses), so: half of it on the circle, and none of the colour just outside (a blob, text)
CIRCLE_HSV = ((3, 20, 165), (22, 110, 255))   # (hue, saturation, value) low / high
CIRCLE_RADIUS = (10, 22)       # px at 1080p
CIRCLE_SUPPORT = 0.5           # share of the outline on the circle
CIRCLE_OUTSIDE = 0.25          # most of the colour 5 px outside it
# ...and no solid orange inside: a waypoint / ping diamond (pale orange edge, solid orange square) passed for one,
# 0.69 of its middle saturated orange (2026-11-05 03:13:59); the real circles 0.0
CIRCLE_FILLED = 0.2

# ---------------------------
# FINDING ICONS
# ---------------------------

_templates = {}


def templates(folder=ICONS_FOLDER):
    if folder not in _templates:
        _templates[folder] = {p.stem: cv2.imread(str(p)) for p in sorted(folder.glob("*.png"))}
    return _templates[folder]


def find_icons(image, scales, threshold, kinds=None, folder=ICONS_FOLDER):
    """[(kind, x, y, size, score)] of map icons in an image (BGR), best first, one per spot."""
    hits = []
    for name, template in templates(folder).items():
        kind = name.split("__")[0]
        if kinds and kind not in kinds:
            continue
        for s in scales:
            t = cv2.resize(template, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if t.shape[0] >= image.shape[0] or t.shape[1] >= image.shape[1]:
                continue
            scores = cv2.matchTemplate(image, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.nonzero(scores >= threshold)
            hits += [(kind, x + t.shape[1] / 2, y + t.shape[0] / 2, t.shape[1], float(scores[y, x])) for y, x in zip(ys, xs)]
    hits.sort(key=lambda h: -h[4])
    kept = []
    for h in hits:
        if all(np.hypot(h[1] - k[1], h[2] - k[2]) > 0.6 * max(h[3], k[3]) for k in kept):
            kept.append(h)
    return kept


def minimap_sightings(minimap, where, margin, when, kinds=ITEMS):
    """[(kind, x, y, when)] on the reference: the items on one placed minimap (`where`: its placement)."""
    out = []
    for kind, x, y, _, _ in find_icons(minimap, SCALES_MINIMAP, ITEM_SCORE, kinds):
        p = where["fit"] @ np.array([x - margin, y - margin, 1.0])
        out.append((kind, float(p[0]), float(p[1]), when))
    return out


def care_package_circles(view, scale=1.0):
    """[(x, y, r)] in view px: the pale orange circles round Care Package landing spots (CIRCLE_*). `scale`: the
    screenshot's size against 1080p."""
    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    low, high = CIRCLE_HSV
    mask = cv2.inRange(hsv, np.array(low, np.uint8), np.array(high, np.uint8))
    found = cv2.HoughCircles(cv2.GaussianBlur(mask, (0, 0), 1.2), cv2.HOUGH_GRADIENT, dp=1, minDist=18 * scale,
                             param1=60, param2=10, minRadius=int(CIRCLE_RADIUS[0] * scale),
                             maxRadius=int(CIRCLE_RADIUS[1] * scale))
    h, w = mask.shape
    angles = np.radians(np.arange(0, 360, 5))
    on = lambda xs, ys: mask[np.clip(ys, 0, h - 1).astype(int), np.clip(xs, 0, w - 1).astype(int)] > 0
    out = []
    for cx, cy, r in (found[0] if found is not None else []):
        # The outline's share on a circle of this radius (+-2 px), counting a hit within 1.5 px either side of it
        best = max((np.mean(np.any([on(cx + (rr + d) * np.cos(angles), cy + (rr + d) * np.sin(angles))
                                    for d in (-1.5, 0, 1.5)], axis=0)), rr) for rr in r + np.arange(-2, 3))
        support, rr = best
        outside = np.mean(on(cx + (rr + 5 * scale) * np.cos(angles), cy + (rr + 5 * scale) * np.sin(angles)))
        ys, xs = np.mgrid[-int(rr):int(rr) + 1, -int(rr):int(rr) + 1]
        middle = hsv[np.clip(ys + int(cy), 0, h - 1), np.clip(xs + int(cx), 0, w - 1)][xs ** 2 + ys ** 2 <= (0.6 * rr) ** 2]
        filled = np.mean((middle[:, 0] >= 5) & (middle[:, 0] <= 30) & (middle[:, 1] > 120) & (middle[:, 2] > 150))
        if support >= CIRCLE_SUPPORT and outside <= CIRCLE_OUTSIDE and filled < CIRCLE_FILLED:
            out.append((float(cx), float(cy), float(rr)))
    return out


def map_sightings(locator, shot, when, kinds=ITEMS):
    """[(kind, x, y, when)] on the reference: the items on one full-map screenshot; None if it can't be placed."""
    import Match_Map
    s = shot.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in Match_Map.MAP_VIEW_BOX)
    view = shot[y1:y2, x1:x2]
    where = locator.locate_map(view)
    if not where:
        return None
    out = []
    for kind, x, y, _, _ in find_icons(view, [k * s for k in SCALES_FULL_MAP], ITEM_SCORE, kinds):
        p = where["fit"] @ np.array([x, y, 1.0])
        out.append((kind, float(p[0]), float(p[1]), when))
    if "care_package" in kinds:
        for x, y, _ in care_package_circles(view, s):
            p = where["fit"] @ np.array([x, y, 1.0])
            out.append(("care_package", float(p[0]), float(p[1]), when))
    return out


def match_items(found, map_shots, locator, margin, ended=None):
    """Items seen in a match, on the reference: [{"kind", "x", "y", "first_seen", "seen"}].
    `found`: [(event, where, minimap)] (placed minimaps); `map_shots`: [(time, screenshot)] of the full map."""
    sightings = []                                # (kind, x, y, time)
    for e, where, minimap in found[::MINIMAP_EVERY]:
        if e["screen"] not in ("in_game", "spectate") or (ended and e["time"] > ended):
            continue
        sightings += minimap_sightings(minimap, where, margin, e["time"])
    for when, shot in map_shots:
        sightings += map_sightings(locator, shot, when) or []
    return group_sightings(sightings)


def group_sightings(sightings):
    """Sightings of one kind within SAME_ITEM px are one item, kept once seen MIN_SIGHTINGS times; first-seen time
    kept (Care Packages land during the match)."""
    items = []
    for kind, x, y, when in sorted(sightings, key=lambda s: s[3]):
        item = next((i for i in items if i["kind"] == kind and
                     np.hypot(np.median(i["xs"]) - x, np.median(i["ys"]) - y) <= SAME_ITEM), None)
        if item:
            item["xs"].append(x), item["ys"].append(y)
        else:
            items.append({"kind": kind, "xs": [x], "ys": [y], "first_seen": when})
    return [{"kind": i["kind"], "x": round(float(np.median(i["xs"])), 1), "y": round(float(np.median(i["ys"])), 1),
             "first_seen": i["first_seen"], "seen": len(i["xs"])} for i in items if len(i["xs"]) >= MIN_SIGHTINGS]

# ---------------------------
# CLEAN BACKGROUND
# ---------------------------

def icon_mask(image, scales, threshold=CLEAN_SCORE, folder=ICONS_FOLDER):
    """255 wherever a map icon is in the image (a little beyond its edge: icons have a glow)."""
    mask = np.zeros(image.shape[:2], np.uint8)
    for _, x, y, size, _ in find_icons(image, scales, threshold, folder=folder):
        r = int(size * 0.7)
        cv2.rectangle(mask, (int(x) - r, int(y) - r), (int(x) + r, int(y) + r), 255, -1)
    return mask


def cached(source, suffix, make):
    """<source stem><suffix>.png beside the source, made again whenever the source is newer."""
    source = Path(source)
    out = source.with_name(f"{source.stem}{suffix}.png")
    if not out.exists() or out.stat().st_mtime < source.stat().st_mtime:
        image = cv2.imread(str(source))
        if image is None:                          # being rewritten right now: the last one will do until it's done
            if out.exists():
                return out
            raise OSError(f"can't read {source}")
        tmp = out.with_name(f".{out.stem}.tmp.png")
        cv2.imwrite(str(tmp), make(image))
        tmp.replace(out)
    return out


def drawing_reference(folder):
    """reference.png with its icons, little coloured dots and printed POI names painted over from their
    surroundings: the map picture drawings use (they print the names themselves). The original stays as it
    is: map positions are found on it."""
    import json
    pois_file = Path(folder) / "pois.json"
    pois = json.loads(pois_file.read_text())["pois"] if pois_file.exists() else []

    def make(image):
        mask = icon_mask(image, SCALES_REFERENCE, REFERENCE_SCORE) | icon_mask(image, SCALES_REFERENCE, CLEAN_SCORE, ICONS_FOLDER / "far")
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        coloured = ((hsv[..., 1] > 120) & (hsv[..., 2] > 150)).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(coloured)
        dots = np.isin(labels, [i for i in range(1, n) if DOT_AREA[0] <= stats[i, cv2.CC_STAT_AREA] <= DOT_AREA[1]])
        mask[cv2.dilate(dots.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0] = 255
        white = ((hsv[..., 1] < 70) & (hsv[..., 2] > 160)).astype(np.uint8)
        text = np.zeros_like(mask)
        for poi in pois:
            w, h = len(poi["name"]) * POI_CHAR[0] / 2 + 6, POI_CHAR[1] / 2
            x1, y1, x2, y2 = (int(round(v)) for v in (poi["x"] - w, poi["y"] - h, poi["x"] + w, poi["y"] + h))
            text[max(0, y1):y2, max(0, x1):x2] = white[max(0, y1):y2, max(0, x1):x2]
        mask[cv2.dilate(text, np.ones((5, 5), np.uint8)) > 0] = 255
        return cv2.inpaint(image, mask, 5, cv2.INPAINT_TELEA) if mask.any() else image
    return cached(Path(folder) / "reference.png", "_clean", make)


def stitched_icons(folder):
    """Where stitched.png has icons (the minimaps it's made of are full of them): drawings show the clean
    reference there instead."""
    return cached(Path(folder) / "stitched.png", "_icons", lambda image: icon_mask(image, SCALES_STITCHED))


def main():
    from Map_Locate import MAP_NAMES
    for name in sys.argv[1:] or MAP_NAMES:
        folder = Path(__file__).parent / "Maps" / name
        for source, make in (("reference.png", drawing_reference), ("stitched.png", stitched_icons)):
            if (folder / source).exists():
                (folder / f"{Path(source).stem}{'_clean' if source == 'reference.png' else '_icons'}.png").unlink(missing_ok=True)
                print(make(folder))


if __name__ == "__main__":
    main()
