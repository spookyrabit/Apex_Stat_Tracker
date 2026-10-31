"""
Where were you on the map? Matches every saved minimap against the map picture for that map, which
gives your path through the match, and from it: the dropship's heading, where you landed (POI), and
where you died (Death Zone). Also reads the kill feed after landing to guess whether the drop was
contested, and keeps building a higher-res map out of your minimaps.

Per map, in Maps/<Map>/:
    reference.png  the map picture every position is measured on
    pois.json      POI name -> position on reference.png (edit by hand to fix or add POIs)
    info.json      the minimap's scale on reference.png (learned from your games)
    stitched.png   high-res map built from your minimaps over time (+ coverage.png: how often seen)

Per match, in Captures/<match>/: path.json (every position) and path.jpg (the path drawn on the map).

Usage:
    python Map_Locate.py --build-assets          # one-time: map pictures + POIs out of the workbook
    python Map_Locate.py --reference-from <full map screenshot> --map "Worlds Edge"
                                                 # new map picture from a zoomed-out map screen (dropship)
    python Map_Locate.py Captures/<match>        # work out one match and print what it found
"""
import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

# ---------------------------
# CONFIGURATION
# ---------------------------

HERE = Path(__file__).parent
MAPS_FOLDER = HERE / "Maps"
MAP_NAMES = ["Worlds Edge", "E-District", "Olympus", "Storm Point", "Broken Moon", "Kings Canyon"]

MINIMAP_MARGIN = 8             # px trimmed off the saved minimap (white frame corners)
MIN_INLIERS = 8                # matched features needed to trust a position
MAX_ROTATION = 3               # degrees; the minimap is north-up, so a rotated fit is a bad fit
SCALE_TOLERANCE = 0.05         # fits must be within 5% of the match's minimap scale (it isn't the same every game)
# Ring radii in game metres once rounds 2-5 have closed. apexlegends.wiki.gg/wiki/Ring says diameters 650 / 400 / 200 /
# 100 m, but your rings measure 320 / 200 / 100 / ~57 m (rounds 3-4 seen dozens of times, round 5 once, 26 Oct), so 320
# and 60; round 6 closes to nothing. Round 1 is a different size on every map, so that one is learned from your games
RING_RADII_M = [320, 200, 100, 60]
MAX_SPEED = 60                 # px/s on the reference; anything faster is a mismatch (dropship is ~15)
JUMP_CONFIRM = 3               # ...unless this many placements in a row agree on the new spot (respawned / redeployed)
TRACK_ZOOM_STEP = 0.35         # a minimap lined up with the one before may be zoomed this much more / less (the
                               # minimap zooms in steps late in the game and while spectating)
TRACK_GAP = 4                  # s; a minimap that can't be placed on the map is lined up with one this recent
LAND_WINDOW = 8                # s; landed = you stay within LAND_RADIUS for this long after jumping
LAND_RADIUS = 20               # px on the reference
LAND_FALLBACK = 40             # s after jumping, when the minimaps are too far apart to see you stop
CONTESTED_WINDOW = (-15, 120)  # s around landing in which a kill feed line with your squad = contested
STITCH_MAX_TINT = 0.03         # skip ring-tinted minimaps (red outside the ring) for the stitched map
STITCH_MEMORY = 30             # stitched pixels are an average of about the last 30 sightings
CLEAN_SCREENS = 25             # map screenshots kept per map for the cleaned-up map picture

# ---------------------------
# MAP ASSETS
# ---------------------------

def squash(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())

def map_folder(map_name):
    return MAPS_FOLDER / map_name

DROPSHIP_MAP_BOX = (455, 0, 1485, 1040)    # the whole map on the full-map screen, zoomed out (1920x1080)

def label_positions(image):
    """POI names printed on a map picture: (text, centre x, centre y)."""
    from Extract_Stats import ocr_engine
    zoom = max(1.0, 2000 / image.shape[1])
    big = cv2.resize(image, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
    result, _ = ocr_engine()(big)
    out = []
    for box, text, _ in result or []:
        if len(squash(text)) >= 4 and not any(ch.isdigit() for ch in text):
            (x1, y1), (x2, y2) = box[0], box[2]
            out.append((text, (x1 + x2) / 2 / zoom, (y1 + y2) / 2 / zoom))
    return out

def history_names(map_name):
    """POI / Death Zone spellings you've used on this map, most used first."""
    import Sheet_Write
    counts = {}
    if Sheet_Write.GOOGLE_EXPORT.exists():
        import openpyxl
        games = openpyxl.load_workbook(Sheet_Write.GOOGLE_EXPORT, read_only=True)["Past Games"]
        for row in games.iter_rows(min_row=2, values_only=True):
            for name in (row[12], row[20]):          # Death Zone, POI
                if isinstance(name, str) and name.strip() and row[15] == map_name:
                    counts[name.strip()] = counts.get(name.strip(), 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

def make_pois(map_name, found, known_first=()):
    """Turn (text, x, y) label sightings into named POIs, using your spellings. A printed label
    you've never typed ("Space Port") is kept as printed."""
    import difflib
    known = []
    for name in list(known_first) + history_names(map_name):
        # Typos ("Bonsi Plaza" / "Bonsai Plaza") fold into whichever spelling came first
        if not any(difflib.SequenceMatcher(None, squash(name), squash(k)).ratio() >= 0.85 for k in known):
            known.append(name)
    def canonical(text):
        s = squash(text)
        for name in known:
            k = squash(name)
            if s == k or (len(s) >= 5 and (s in k or k in s)):
                return name
        close = difflib.get_close_matches(s, [squash(n) for n in known], n=1, cutoff=0.75)
        return next(n for n in known if squash(n) == close[0]) if close else None
    pois = {}
    for text, x, y in found:
        name = canonical(text)
        if not name and len(squash(text)) >= 6 and re.fullmatch(r"[A-Za-z' ]+", text.strip()):
            name = text.strip().title()
            known.append(name)
        if name:
            pois.setdefault(name, []).append((x, y))      # split words / two sources: average them
    return [{"name": name, "x": round(float(np.mean([p[0] for p in pts])), 1),
             "y": round(float(np.mean([p[1] for p in pts])), 1)} for name, pts in sorted(pois.items())]

def save_pois(map_name, pois, size):
    with open(map_folder(map_name) / "pois.json", "w") as f:
        json.dump({"map": map_name, "reference_size": list(size), "pois": pois}, f, indent=2)

def build_assets(remove_images=False):
    """Map pictures out of the workbook into Maps/, plus POI positions from the names typed over
    them in the workbook, the names printed on the pictures, and the spellings used in Past Games."""
    import openpyxl
    import Sheet_Write
    export = openpyxl.load_workbook(Sheet_Write.GOOGLE_EXPORT)
    typed = Sheet_Write.run_job({"action": "map_assets", "workbook": str(Sheet_Write.WORKBOOK),
                                 "sheets": MAP_NAMES, "remove_images": remove_images})["maps"]
    for map_name in MAP_NAMES:
        folder = map_folder(map_name)
        folder.mkdir(parents=True, exist_ok=True)
        picture = export[map_name]._images[0]
        reference = cv2.imdecode(np.frombuffer(picture._data(), np.uint8), cv2.IMREAD_COLOR)
        cv2.imwrite(str(folder / "reference.png"), reference)
        h, w = reference.shape[:2]
        grid = [(name, u * w, v * h) for name, u, v in (typed.get(map_name) or {}).get("labels", [])
                if 0 <= u <= 1 and 0 <= v <= 1]      # skip tabs whose grid doesn't sit on the picture
        pois = make_pois(map_name, grid + label_positions(reference), known_first=[n for n, _, _ in grid])
        save_pois(map_name, pois, (w, h))
        print(f"{map_name}: {w}x{h} map, {len(pois)} POIs")

def reference_from(screenshot, map_name, box=DROPSHIP_MAP_BOX):
    """Use a full-map screenshot (zoomed out, e.g. from the dropship) as the map picture. For maps
    whose old picture doesn't match the game any more. Positions start over on the new picture."""
    image = cv2.imread(str(screenshot))
    if image is None:
        raise SystemExit(f"Can't read {screenshot}")
    s = image.shape[1] / 1920
    x1, y1, x2, y2 = (int(round(v * s)) for v in box)
    reference = image[y1:y2, x1:x2]
    folder = map_folder(map_name)
    folder.mkdir(parents=True, exist_ok=True)
    old = []
    if (folder / "reference.png").exists():
        (folder / "reference.png").unlink()
        if (folder / "pois.json").exists():
            old = [p["name"] for p in json.loads((folder / "pois.json").read_text())["pois"]]
    for stale in ("info.json", "stitched.png", "coverage.png", "features.npz", "stitched_features.npz"):
        (folder / stale).unlink(missing_ok=True)        # all measured on the old picture
    for shot in (folder / "screens").glob("*.png") if (folder / "screens").exists() else []:
        shot.unlink()
    cv2.imwrite(str(folder / "reference.png"), reference)
    cv2.imwrite(str(folder / "reference_base.png"), reference)       # the new original
    pois = make_pois(map_name, label_positions(reference), known_first=old)
    save_pois(map_name, pois, reference.shape[1::-1])
    print(f"{map_name}: new {reference.shape[1]}x{reference.shape[0]} map, {len(pois)} POIs: "
          + ", ".join(p["name"] for p in pois))

def add_map_screens(locator, screenshots):
    """Line full-map screenshots up with the map picture and keep them (Maps/<Map>/screens/), then
    rebuild reference.png as the per-pixel median of all of them. Rings, red tint, the dropship line and
    the cursor are different in every screenshot, so they drop out; the map itself stays. Positions on
    the picture don't change, so POIs and earlier paths stay valid."""
    import Match_Map
    screens = locator.folder / "screens"
    screens.mkdir(exist_ok=True)
    base = locator.folder / "reference_base.png"
    if not base.exists():                          # the picture everything is measured on, before cleaning
        cv2.imwrite(str(base), locator.reference)
    h, w = locator.reference.shape[:2]
    added = 0
    for name, shot, before_rings in screenshots:
        # Shots from the dropship (before the first ring) are kept apart: they're the clean ones
        name = f"{name}_dropship" if before_rings else name
        if (screens / f"{name}.png").exists():
            continue
        s = shot.shape[1] / 1920
        x1, y1, x2, y2 = (int(round(v * s)) for v in Match_Map.MAP_VIEW_BOX)
        view = shot[y1:y2, x1:x2]
        where = locator.locate_image(view)
        if not where or where["inliers"] < 20:
            continue
        warped = cv2.warpAffine(view, where["fit"], (w, h), flags=cv2.INTER_AREA, borderValue=(0, 0, 0))
        seen = cv2.warpAffine(np.full(view.shape[:2], 255, np.uint8), where["fit"], (w, h), flags=cv2.INTER_NEAREST)
        cv2.imwrite(str(screens / f"{name}.png"), np.dstack([warped, seen]))     # BGRA: alpha = covered
        added += 1
    if added:
        clean_reference(locator)
    prune_screens(screens)
    return added

def px_per_metre(sizes):
    """The map picture's scale (px per game metre) from its learned ring sizes: the scale at which the most of
    them are rounds 2-5 (RING_RADII_M, within 12%); None unless at least two are."""
    best = None
    for s in sizes:
        for m in RING_RADII_M:
            k = s / m
            pairs = [(t, mm) for t in sizes for mm in RING_RADII_M if abs(t - k * mm) <= 0.12 * k * mm]
            if len(pairs) >= 2 and (best is None or len(pairs) > len(best)):
                best = pairs
    return round(float(np.median([t / mm for t, mm in best])), 4) if best else None

def prune_screens(screens, keep_dropship=CLEAN_SCREENS, keep_other=10):
    """Only the newest shots are ever used to clean the map: delete the rest (they're 1-3 MB each)."""
    shots = sorted(screens.glob("*.png"))
    dropship = [f for f in shots if f.stem.endswith("_dropship")]
    other = [f for f in shots if not f.stem.endswith("_dropship")]
    for f in dropship[:-keep_dropship] + other[:-keep_other]:
        f.unlink()

def overlay_mask(image):
    """Where a map screenshot has something drawn over the map: the next-ring disc, the red tint
    outside the ring, the cursor's long thin lines. Those pixels don't get a say in the clean map."""
    import Match_Map
    h, w = image.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    disc = Match_Map.brighter_disc(image, min_share=0.55)
    if disc:
        cv2.circle(mask, (int(disc[0]), int(disc[1])), int(disc[2]) + 6, 255, -1)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    for tint in ((((hsv[..., 0] < 8) | (hsv[..., 0] > 172)) & (hsv[..., 1] > 90) & (hsv[..., 2] > 35)),   # outside the ring
                 ((hsv[..., 0] >= 80) & (hsv[..., 0] <= 100) & (hsv[..., 1] > 80) & (hsv[..., 2] > 60))):  # teal next ring
        wide = cv2.morphologyEx(tint.astype(np.uint8), cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))   # not lava / icons
        mask[wide > 0] = 255
    # The cursor: a full-length row / column a bit brighter than the ones beside it
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    for axis in (0, 1):
        g = gray if axis == 0 else gray.T
        lift = g[2:-2] - (g[:-4] + g[4:]) / 2           # each row against the rows 2 above / below
        for i in np.nonzero((lift > 12).mean(axis=1) > 0.5)[0]:
            if axis == 0:
                mask[max(0, i - 2):i + 7] = 255
            else:
                mask[:, max(0, i - 2):i + 7] = 255
    # The dropship's flight line: long and straight, orange
    orange = ((hsv[..., 0] >= 5) & (hsv[..., 0] <= 25) & (hsv[..., 1] > 100) & (hsv[..., 2] > 100)).astype(np.uint8) * 255
    found = cv2.HoughLinesP(orange, 1, np.pi / 360, 80, minLineLength=150, maxLineGap=12)
    for x1, y1, x2, y2 in ([] if found is None else found.reshape(-1, 4)):
        cv2.line(mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, 9)
    return mask > 0

def clean_reference(locator):
    # Dropship shots never have a ring on them: with a few of those, use only them
    shots = sorted((locator.folder / "screens").glob("*_dropship.png"))[-CLEAN_SCREENS:]
    if len(shots) < 2:
        shots = sorted((locator.folder / "screens").glob("*.png"))[-CLEAN_SCREENS:]
    base = cv2.imread(str(locator.folder / "reference_base.png"))
    layers, plain = [], []
    pictures = [(base, np.ones(base.shape[:2], bool))]
    for f in shots:
        img = cv2.imread(str(f), cv2.IMREAD_UNCHANGED)
        image, seen = img[..., :3], img[..., 3] > 0
        if np.abs(image[seen].astype(np.int16) - base[seen]).mean() < 4:
            continue                               # the screenshot the base picture was made from
        if seen.mean() < 0.5:
            continue                               # zoomed in: drawn with different detail, leaves seams
        pictures.append((image, seen))
    for image, seen in pictures:
        layer = image.astype(np.float32)
        layer[~seen] = np.nan
        plain.append(layer.copy())
        layer[overlay_mask(image)] = np.nan
        layers.append(layer)
    if len(layers) < 3:                            # a median of two isn't a vote yet
        cv2.imwrite(str(locator.folder / "reference.png"), base)
        return
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)          # all-NaN pixels are filled below
        clean = np.nanmedian(np.stack(layers), axis=0)
        # Pixels every screenshot had something over: fall back to the plain median
        fallback = np.nanmedian(np.stack(plain), axis=0)
    clean = np.where(np.isnan(clean), fallback, clean)
    cv2.imwrite(str(locator.folder / "reference.png"), np.clip(clean, 0, 255).astype(np.uint8))

# ---------------------------
# LOCATING
# ---------------------------

def prep(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)

def tint_fraction(image):
    """How much of a minimap is washed red (outside the ring) or teal (ring overlays)."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    tinted = (s > 110) & (v > 50) & ((h < 10) | (h > 170) | ((h > 80) & (h < 100)))
    return float(tinted.mean())

def fit_matches(keypoints, descriptors, points, train, matcher, shape, centre=True):
    """Fit the crop's features onto a set of features (north-up: scale + shift only)."""
    pairs = matcher.knnMatch(descriptors, train, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < MIN_INLIERS:
        return None
    src = np.float32([keypoints[g.queryIdx].pt for g in good])
    dst = points[[g.trainIdx for g in good]]
    fit, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=5)
    if fit is None or inliers.sum() < MIN_INLIERS:
        return None
    scale = float(np.hypot(fit[0, 0], fit[1, 0]))
    rotation = float(np.degrees(np.arctan2(fit[1, 0], fit[0, 0])))
    if abs(rotation) > MAX_ROTATION or scale < 0.2:
        return None                               # rotated or collapsed: not a real match
    x, y = fit @ np.array([shape[1] / 2, shape[0] / 2, 1.0])
    return {"x": float(x), "y": float(y), "scale": scale, "inliers": int(inliers.sum()), "fit": fit}

class MapLocator:
    def __init__(self, map_name):
        self.map_name = map_name
        self.folder = map_folder(map_name)
        picture, base = self.folder / "reference.png", self.folder / "reference_base.png"
        if not picture.exists() and base.exists():
            # reference.png is rebuilt from your screenshots and isn't in git: start from the original
            picture.write_bytes(base.read_bytes())
        self.reference = cv2.imread(str(picture))
        if self.reference is None:
            raise FileNotFoundError(f"No map picture for {map_name} (Maps/{map_name}/reference.png)")
        with open(self.folder / "pois.json") as f:
            self.pois = json.load(f)["pois"]
        self.info_file = self.folder / "info.json"
        self.info = json.loads(self.info_file.read_text()) if self.info_file.exists() else {}
        self.sift = cv2.SIFT_create()
        self.matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 64})
        # Matched against the map picture first, then the stitched minimaps (your own minimaps at their
        # own resolution: far more to match on wherever you've been before)
        self.sources = [self.features(self.folder / "reference.png", self.folder / "features.npz", 1.0)]
        if self.stitch_zoom and (self.folder / "stitched.png").exists():
            self.sources.append(self.features(self.folder / "stitched.png", self.folder / "stitched_features.npz",
                                              self.stitch_zoom))

    def features(self, picture, cache, zoom):
        """SIFT features of a picture (positions in reference px), cached next to it (they take seconds)."""
        if cache.exists() and cache.stat().st_mtime >= picture.stat().st_mtime:
            data = np.load(cache)
            return data["points"], data["descriptors"]
        image = cv2.imread(str(picture))
        keypoints, descriptors = self.sift.detectAndCompute(prep(image), None)
        points = np.float32([k.pt for k in keypoints]) / zoom
        np.savez(cache, points=points, descriptors=descriptors)
        return points, descriptors

    @property
    def ring_sizes(self):
        """The map's ring radii (px on the reference): round 1 as seen in your games (bigger rings learned too),
        rounds 2-5 from their size in metres x this map's px per metre - so a size caught mid-close can't
        become a "size", and the small late rings are known before you've seen them. Before the scale is known:
        the learned sizes as they are."""
        learned, k = self.info.get("ring_sizes", []), self.info.get("px_per_m")
        if not k:
            return learned
        return sorted([s for s in learned if s > RING_RADII_M[0] * k * 1.25] + [round(m * k, 1) for m in RING_RADII_M])

    def ring_round(self, r):
        """(round, radius in game metres) of a ring this many px across, or (None, measured metres) if it's
        none of the map's ring sizes (or (None, None) before the map's scale is known)."""
        k = self.info.get("px_per_m")
        if not k:
            return None, None
        sizes = sorted(self.ring_sizes, reverse=True)
        for i, s in enumerate(sizes):
            if abs(r - s) <= max(0.12 * s, 4):
                stage = i - (len(sizes) - len(RING_RADII_M))    # rounds 2-5 are the last four sizes
                return i + 1, (RING_RADII_M[stage] if stage >= 0 else round(s / k))
        return None, round(r / k)

    @property
    def stitch_zoom(self):
        """Stitched map px per reference px (set from the first match, then fixed)."""
        return self.info.get("stitch_zoom")

    def locate(self, minimap):
        """Your position on the reference (the middle of the minimap), or None if it can't be placed."""
        m = MINIMAP_MARGIN
        return self.locate_image(minimap[m:-m, m:-m])

    def locate_image(self, crop):
        """Where a piece of map (minimap, zoomed map screen) sits on the reference: the fit from its
        pixels to the reference, its scale, and where its middle lands. None if it can't be placed."""
        keypoints, descriptors = self.sift.detectAndCompute(prep(crop), None)
        if descriptors is None or len(keypoints) < MIN_INLIERS:
            return None
        for points, train in self.sources:
            where = fit_matches(keypoints, descriptors, points, train, self.matcher, crop.shape)
            if where:
                return where
        return None

    def place(self, minimap, when, last=None):
        """Where a minimap is: matched on the map picture, else lined up with the last placed one
        (`last`: (when, minimap, fit, where) or None). Late in a match the tint leaves little to match and a
        patch can match a wrong spot far away: a placement further from the last one than anyone can move
        is checked by lining the two minimaps up, and if they do line up that wins (a real jump - respawning,
        spectating someone else - doesn't line up)."""
        recent = last and (when - last[0]).total_seconds() <= TRACK_GAP
        where = self.locate(minimap)
        if where and recent:
            dt = max((when - last[0]).total_seconds(), 1)
            if np.hypot(where["x"] - last[3]["x"], where["y"] - last[3]["y"]) > MAX_SPEED * dt:
                where = self.track(last[1], last[2], minimap) or where
        elif not where and recent:
            where = self.track(last[1], last[2], minimap) or self.bridge(last[1], last[2], minimap)
        return where

    def bridge(self, previous, previous_fit, minimap):
        """Place a minimap by the ring hole on it and on the previous (placed) one: a second apart it's the same
        ring, so its size gives the new zoom and its middle the position. Late in a match the minimap zooms
        in a long way at once and shows little but the hole, so nothing else lines up across that jump."""
        import Match_Map
        m = MINIMAP_MARGIN
        crop, before = minimap[m:-m, m:-m], previous[m:-m, m:-m]
        old, new = Match_Map.ring_from_hole(before), Match_Map.ring_from_hole(crop)
        if not old or not new or not 0.25 <= old[2] / new[2] <= 4:
            return None                            # no hole on one of them, or an impossible zoom change
        centre = previous_fit @ np.array([old[0], old[1], 1.0])
        scale = float(np.hypot(previous_fit[0, 0], previous_fit[1, 0])) * old[2] / new[2]
        fit = np.array([[scale, 0.0, centre[0] - scale * new[0]], [0.0, scale, centre[1] - scale * new[1]]])
        x, y = fit @ np.array([crop.shape[1] / 2, crop.shape[0] / 2, 1.0])
        return {"x": float(x), "y": float(y), "scale": scale, "inliers": 0, "fit": fit, "tracked": True, "bridged": True}

    def track(self, previous, previous_fit, minimap):
        """Place a minimap by lining it up with the previous (placed) one: a second apart they overlap
        almost completely, even where the map picture has too little to match on."""
        m = MINIMAP_MARGIN
        crop, before = minimap[m:-m, m:-m], previous[m:-m, m:-m]
        kp, desc = self.sift.detectAndCompute(prep(crop), None)
        kp0, desc0 = self.sift.detectAndCompute(prep(before), None)
        if desc is None or desc0 is None or len(kp) < MIN_INLIERS or len(kp0) < MIN_INLIERS:
            return None
        points0 = np.float32([k.pt for k in kp0])
        step = fit_matches(kp, desc, points0, desc0, self.matcher, crop.shape, centre=False)
        if not step or abs(step["scale"] - 1) > TRACK_ZOOM_STEP:
            return None
        fit = np.vstack([previous_fit, [0, 0, 1]]) @ np.vstack([step["fit"], [0, 0, 1]])
        fit = fit[:2]
        x, y = fit @ np.array([crop.shape[1] / 2, crop.shape[0] / 2, 1.0])
        return {"x": float(x), "y": float(y), "scale": float(np.hypot(fit[0, 0], fit[1, 0])),
                "inliers": step["inliers"], "fit": fit, "tracked": True}

    def learn_ring_sizes(self, rings, match):
        """Remember the ring radii each game saw on this map (per game, so redrawing a game doesn't count
        it twice); sizes seen in at least two games (within 5%) are the map's ring sizes, used to fit
        rings that are only partly on screen."""
        seen = self.info.get("ring_radii_by_match", {})
        seen[match] = [round(r["r"], 1) for r in rings if r["kind"] in ("ring", "next ring") and not r.get("partial")]
        self.info["ring_radii_by_match"] = seen
        self.info.pop("ring_radii", None)
        groups = []                                # [(radius, games it was seen in)]
        for r, game in sorted((r, g) for g, radii in seen.items() for r in radii):
            if groups and r <= groups[-1][0][-1] * 1.05:
                groups[-1][0].append(r)
                groups[-1][1].add(game)
            else:
                groups.append(([r], {game}))
        self.info["ring_sizes"] = [round(float(np.median(rs)), 1) for rs, games in groups if len(games) >= 2]
        k = px_per_metre(self.info["ring_sizes"])
        if k:
            self.info["px_per_m"] = k
        self.info_file.write_text(json.dumps(self.info, indent=2))

    def set_stitch_zoom(self, scale):
        if self.stitch_zoom:
            return
        self.info["stitch_zoom"] = round(1 / scale, 4)
        self.info_file.write_text(json.dumps(self.info, indent=2))

    def nearest_poi(self, x, y):
        if not self.pois:
            return None
        poi = min(self.pois, key=lambda p: (p["x"] - x) ** 2 + (p["y"] - y) ** 2)
        return poi["name"]

    # ---- stitched map ----
    def stitch(self, sightings):
        """Fold located minimaps into Maps/<Map>/stitched.png, at the minimap's own (higher) resolution."""
        if not self.stitch_zoom or not sightings:
            return
        zoom = self.stitch_zoom
        h, w = (int(round(v * zoom)) for v in self.reference.shape[:2])
        stitched_file, coverage_file = self.folder / "stitched.png", self.folder / "coverage.png"
        mean = cv2.imread(str(stitched_file)).astype(np.float32) if stitched_file.exists() else np.zeros((h, w, 3), np.float32)
        count = cv2.imread(str(coverage_file), cv2.IMREAD_GRAYSCALE).astype(np.float32) if coverage_file.exists() else np.zeros((h, w), np.float32)
        m = MINIMAP_MARGIN
        for minimap, fit in sightings:
            crop = minimap[m:-m, m:-m].astype(np.float32)
            size = fit[0, 0] * zoom                  # crop px -> stitched px (about 1)
            crop = cv2.resize(crop, None, fx=size, fy=size, interpolation=cv2.INTER_LINEAR)
            x0, y0 = (int(round(v * zoom)) for v in fit[:, 2])
            x1, y1 = min(w, x0 + crop.shape[1]), min(h, y0 + crop.shape[0])
            if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
                continue
            crop = crop[:y1 - y0, :x1 - x0]
            n = np.minimum(count[y0:y1, x0:x1], STITCH_MEMORY)[..., None]
            mean[y0:y1, x0:x1] += (crop - mean[y0:y1, x0:x1]) / (n + 1)
            count[y0:y1, x0:x1] = np.minimum(count[y0:y1, x0:x1] + 1, 255)
        cv2.imwrite(str(stitched_file), np.clip(mean, 0, 255).astype(np.uint8))
        cv2.imwrite(str(coverage_file), count.astype(np.uint8))

# ---------------------------
# MATCH ANALYSIS
# ---------------------------

def seconds(text):
    m, s = (int(v) for v in text.split(":"))
    return m * 60 + s

def scale_levels(scales):
    """Group fit scales into the minimap's zoom levels: [(scale, how many frames)], noise left out."""
    groups = []
    for scale in sorted(scales):
        if groups and scale <= groups[-1][-1] * (1 + SCALE_TOLERANCE):
            groups[-1].append(scale)
        else:
            groups.append([scale])
    enough = 3                                  # the minimap zooms in late in a match / while spectating
    return [(float(np.median(g)), len(g)) for g in groups if len(g) >= enough]

def heading_of(points):
    """Compass heading (0 = north, 90 = east) of a straight run of points."""
    xs, ys = np.array([p["x"] for p in points]), np.array([p["y"] for p in points])
    if len(points) < 3 or np.hypot(xs[-1] - xs[0], ys[-1] - ys[0]) < 15:
        return None
    return int(round(np.degrees(np.arctan2(xs[-1] - xs[0], -(ys[-1] - ys[0]))) % 360))

def find_landing(path, jump):
    """First moment after jumping that you stay put for LAND_WINDOW seconds. With sparse minimaps
    (older captures) fall back to where you were LAND_FALLBACK seconds after jumping."""
    ground = [p for p in path if p["t"] >= jump + 3 and p["screen"] == "in_game"]
    for i, p in enumerate(ground):
        later = [q for q in ground[i:] if q["t"] <= p["t"] + LAND_WINDOW]
        if later[-1]["t"] - p["t"] >= LAND_WINDOW - 2 and all(
                np.hypot(q["x"] - p["x"], q["y"] - p["y"]) <= LAND_RADIUS for q in later):
            return p
    late = [p for p in ground if p["t"] >= jump + LAND_FALLBACK]
    return late[0] if late else None

def steady_move(a, b):
    """Could you get from path point a to b in the time between them (no further than MAX_SPEED)?"""
    dt = (datetime.fromisoformat(b["time"]) - datetime.fromisoformat(a["time"])).total_seconds()
    return np.hypot(b["x"] - a["x"], b["y"] - a["y"]) <= MAX_SPEED * max(dt, 1)

def death_time(stats, log_events):
    """When you went down for good: summary survival times, else the end of the in-match screens."""
    summary = stats.get("summary") or {}
    players = summary.get("players", [])
    from Extract_Stats import is_me
    me = next((p for p in players if is_me(p.get("name"))), None)
    times = [seconds(p["time"]) for p in players if p.get("time")]
    if me and me.get("time") and times and stats.get("ended"):
        # The summary shows up when the last of the squad dies, so count back from there
        return datetime.fromisoformat(stats["ended"]) - timedelta(seconds=max(times) - seconds(me["time"]))
    # No stats screen: when you started spectating for good (not a death you were brought back from),
    # else the end of the in-match screens
    played = [e["time"] for e in log_events if e["screen"] == "in_game" and e["event"] == "enter"]
    last_alive = played[-1] if played else ""
    final = [e["time"] for e in log_events if e["screen"] == "spectate" and e["event"] == "enter" and e["time"] > last_alive]
    if final:
        return datetime.fromisoformat(final[0])
    ends = [e["time"] for e in log_events if e["screen"] == "in_game" and e["event"] == "leave"]
    return datetime.fromisoformat(ends[-1]) if ends else None

def alive_at_end(log_events):
    """True if you weren't spectating at the end (no spectating after your last time in the match)."""
    played = [e["time"] for e in log_events if e["screen"] == "in_game" and e["event"] == "enter"]
    return bool(played) and not any(e["screen"] == "spectate" and e["event"] == "enter" and e["time"] > played[-1]
                                    for e in log_events)

def draw_path(locator, path, landing, death, out_file):
    image = locator.reference.copy()
    colours = {"dropship": (255, 160, 60), "in_game": (255, 255, 255)}
    for a, b in zip(path, path[1:]):
        if b["t"] - a["t"] <= 20:
            cv2.line(image, (int(a["x"]), int(a["y"])), (int(b["x"]), int(b["y"])), colours.get(b["screen"], (200, 200, 200)), 2)
    for point, colour, label in ((landing, (80, 255, 80), "landed"), (death, (60, 60, 255), "died")):
        if point:
            cv2.circle(image, (int(point["x"]), int(point["y"])), 9, colour, 3)
            cv2.putText(image, label, (int(point["x"]) + 12, int(point["y"]) + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)
    cv2.imwrite(str(out_file), image, [cv2.IMWRITE_JPEG_QUALITY, 85])

# Squad chat / pings share the kill feed's corner of the screen; these words mean it isn't a kill
# (long, specific ones: they still match when OCR slips a letter elsewhere, and aren't in player names)
NOT_KILLS = ["DIBS", "SUGGEST", "LOCATION", "PINGED", "CANCELED", "REVEALED", "DROPPINGSOLO", "WELCOME",
             "KILLLEADER", "ULTIMATE", "REQUEST", "ONMYWAY", "THANK", "LOOKINGFOR", "SPOTTED", "ATTACKING",
             "DEFENDING", "WATCHING", "GOINGHERE", "ISREADY", "CHAMPION", "JUMPMASTER", "BEACON", "BANNER",
             "HOLDING", "LOOTING"]

def kill_feed_lines(boxes):
    """OCR boxes -> text lines (boxes at the same height belong together: killer, weapon, victim)."""
    lines = []
    for x, y, text in sorted(boxes, key=lambda b: b[1]):
        if lines and abs(lines[-1][0] - y) <= 10:
            lines[-1][1].append((x, text))
        else:
            lines.append([y, [(x, text)]])
    return [" ".join(t for _, t in sorted(parts)) for _, parts in lines]

def is_fight_line(line, squad_names):
    """A kill feed line between someone in your squad and someone who isn't."""
    from Extract_Stats import squash as squash_text
    if ":" in line or any(word in squash_text(line) for word in NOT_KILLS):
        return False
    rest, ours = squash_text(re.sub(r"\[[^\]]*\]", " ", line)), False    # club tags aren't names
    for name in sorted(squad_names, key=len, reverse=True):     # "Dong_Ki_Kongg" before "Dong_Ki"
        key = squash_text(name)
        for size in range(len(key), 5, -1):          # names get cut off / misread at the end
            if key[:size] in rest:
                rest, ours = rest.replace(key[:size], "", 1), True
                break
    rest = re.sub(r"BLEEDOUT|KNOCKED|ELIMINATED", "", rest)
    return ours and len(re.sub(r"[^A-Z]", "", rest)) >= 4    # someone else is named too

def read_contested(folder, events, squad_names, landed_at):
    """Was the drop contested (another squad landed at your POI)? Evidence soon after landing: the game's
    "Squad landed nearby!", someone in your squad spotting an enemy, breaking an enemy's shield, or a
    kill feed line between your squad and another. Keeps the lines that decided it."""
    from Extract_Stats import read_text, squash
    lo, hi = (landed_at + timedelta(seconds=s) for s in CONTESTED_WINDOW)
    evidence, seen, read = [], set(), 0
    for e in events:
        if e["event"] != "killfeed" or not lo <= datetime.fromisoformat(e["time"]) <= hi:
            continue
        image = cv2.imread(str(folder / e["file"]))
        if image is None or cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 2].max() < 200:
            continue                               # no bright text, nothing in the feed
        read += 1
        for line in kill_feed_lines(read_text(image)):
            key = squash(line)
            if "LANDEDNEARBY" in key:
                why = "squad landed nearby"        # the game's own call-out
            elif "SPOTTEDANENEMY" in key:
                why = "enemy spotted"              # someone in your squad saw another squad
            elif "SHIELDBROKEN" in key:
                why = "enemy shield broken"        # your squad hit someone
            elif is_fight_line(line, squad_names):
                why = "fight"
            else:
                continue
            if key not in seen:                    # each kill feed line shows for several seconds
                seen.add(key)
                evidence.append({"time": e["time"], "why": why, "line": line})
    reasons = sorted({e["why"] for e in evidence})
    return {"contested": bool(evidence), "reasons": reasons, "evidence": evidence, "feeds_read": read}

def read_squads_left(folder, events, until):
    """The last "N SQUADS LEFT" seen before `until`: your placement if the stats screen was missed."""
    from Extract_Stats import read_text
    shots = [e for e in events if e["event"] == "hud" and datetime.fromisoformat(e["time"]) <= until]
    for e in reversed(shots[-5:]):
        image = cv2.imread(str(folder / e["file"]))
        if image is None:
            continue
        big = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        for _, _, text in sorted(read_text(big)):
            m = re.fullmatch(r"\d{1,2}", text.strip())
            if m and 1 <= int(m.group()) <= 30:
                return int(m.group())
    return None

def analyse_match(folder, stats):
    """Everything location-based for one match; also saves path.json / path.jpg in the match folder."""
    folder = Path(folder)
    if (folder / ".cleaned").exists():
        # Its minimaps are deleted: working it out again would only wipe what was found before
        return stats.get("location") or {}
    map_name = stats.get("map")
    if map_name not in MAP_NAMES:
        return {"error": f"no map picture for {map_name!r}"}
    with open(folder / "match.json") as f:
        events = json.load(f)["events"]
    locator = MapLocator(map_name)
    shots = [e for e in events if e["event"] == "minimap"]

    found, scales, last, missed = [], [], None, []
    for e in shots:
        minimap = cv2.imread(str(folder / e["file"]))
        if minimap is None:
            continue
        when = datetime.fromisoformat(e["time"])
        where = locator.place(minimap, when, last and (last[0], last[1], last[2]["fit"], last[2]))
        if where and where["scale"] < 0.3 and not where.get("tracked"):
            where = None                           # a degenerate fit, not a place (lined-up ones zoom in further)
        if where:
            found.append((e, where, minimap))
            scales.append(where["scale"])
            last = (when, minimap, where)
        else:
            missed.append((e, minimap))
    # Second pass, backwards: a minimap that couldn't be placed is lined up with the next one that was
    placed_at = {f[0]["time"]: f for f in found}
    for e, minimap in reversed(missed):
        when = datetime.fromisoformat(e["time"])
        after = next((f for f in sorted(found, key=lambda f: f[0]["time"])
                      if 0 < (datetime.fromisoformat(f[0]["time"]) - when).total_seconds() <= TRACK_GAP), None)
        if not after:
            continue
        step = locator.track(after[2], after[1]["fit"], minimap)
        if step:
            found.append((e, step, minimap))
            scales.append(step["scale"])
    found.sort(key=lambda f: f[0]["time"])
    # The minimap's zoom (it can differ between games, or change mid-match): every zoom level that
    # shows up consistently is real; fits at any other scale are mismatches
    solid = [w["scale"] for _, w, _ in found if w["inliers"] >= 15 and not w.get("tracked")] or scales
    levels = scale_levels(solid)
    match_scale = max(levels, key=lambda l: l[1])[0] if levels else None
    if levels:
        # (minimaps lined up with a placed neighbour keep their zoom: late in a match the minimap zooms
        # in further than any minimap that could be matched on the map picture directly)
        found = [f for f in found if f[1].get("tracked")
                 or any(abs(f[1]["scale"] / level - 1) <= SCALE_TOLERANCE for level, _ in levels)]
        locator.set_stitch_zoom(min(level for level, _ in levels))     # the most detailed zoom

    # Drop teleports (a mismatch that slipped through) - but a real jump (respawning far away, Wildcard's instant
    # respawns, watching a teammate) is believed once JUMP_CONFIRM placements in a row agree on the new spot
    import Match_Map
    path, sightings, jumped = [], [], []
    def keep(point, where, minimap):
        path.append(point)
        if tint_fraction(minimap) <= STITCH_MAX_TINT and Match_Map.violet_mask(minimap).mean() < 0.02:
            sightings.append((minimap, where["fit"]))
    for e, where, minimap in found:
        if e["screen"] == "after_squad":
            continue                               # another squad's minimap (after yours was out): rings only
        point = {"time": e["time"], "t": 0.0, "x": round(where["x"], 1), "y": round(where["y"], 1),
                 "inliers": where["inliers"], "screen": e["screen"]}
        if path and not steady_move(path[-1], point):
            if jumped and not steady_move(jumped[-1][0], point):
                jumped = []
            jumped.append((point, where, minimap))
            if len(jumped) >= JUMP_CONFIRM:
                jumped[0][0]["gap"] = True
                for args in jumped:
                    keep(*args)
                jumped = []
            continue
        jumped = []
        keep(point, where, minimap)
    start = datetime.fromisoformat(path[0]["time"]) if path else None
    for p in path:
        p["t"] = (datetime.fromisoformat(p["time"]) - start).total_seconds()

    out = {"map": map_name, "located": len(path), "minimaps": len(shots),
           "minimap_scale": round(match_scale, 3) if match_scale else None}
    # The flight: the dropship before the first jump (a respawn mode can show the dropship again later)
    first_jump = next((p["time"] for p in path if p["screen"] == "in_game"), None)
    ship = [p for p in path if p["screen"] == "dropship" and (not first_jump or p["time"] < first_jump)]
    out["dropship_heading"] = heading_of(ship)
    jumps = [e["time"] for e in events if e["screen"] == "in_game" and e["event"] == "enter"]
    landing = None
    if jumps and path:
        jump = (datetime.fromisoformat(jumps[0]) - start).total_seconds()
        landing = find_landing(path, jump)
    died_at = death_time(stats, events)
    death = None
    if died_at and path:
        before = [p for p in path if p["screen"] == "in_game" and datetime.fromisoformat(p["time"]) <= died_at]
        death = before[-1] if before else None
    if landing:
        out["landing"] = {"time": landing["time"], "x": landing["x"], "y": landing["y"],
                          "poi": locator.nearest_poi(landing["x"], landing["y"])}
    if death:
        out["death"] = {"time": death["time"], "x": death["x"], "y": death["y"],
                        "zone": locator.nearest_poi(death["x"], death["y"])}
    # Death Zone = where the last surviving member of the squad died: the last place the minimap showed
    # before the squad was out (after you go down it follows the teammate you're spectating)
    over = datetime.fromisoformat(stats["ended"]) if stats.get("ended") else None
    final = [w for e, w, _ in found if e["screen"] in ("in_game", "spectate")
             and (not over or datetime.fromisoformat(e["time"]) <= over)]
    last = next((e for e, w, _ in reversed(found) if e["screen"] in ("in_game", "spectate")
                 and (not over or datetime.fromisoformat(e["time"]) <= over)), None)
    if final and last:
        w = final[-1]
        out["squad_out"] = {"time": last["time"], "x": round(w["x"], 1), "y": round(w["y"], 1),
                            "zone": locator.nearest_poi(w["x"], w["y"]),
                            "who": "teammate" if last["screen"] == "spectate" else "you"}

    from Extract_Stats import squad_names
    names = squad_names(stats)
    if landing and names:
        out["contested"] = read_contested(folder, events, names, datetime.fromisoformat(landing["time"]))
    if not stats.get("summary") and died_at:
        out["squads_left_at_end"] = read_squads_left(folder, events, died_at)
        # Losses always show the summary we recognise; a win's screens aren't recognised yet. Alive at
        # the end, down to the last two squads and no summary: you won
        if alive_at_end(events) and out["squads_left_at_end"] and out["squads_left_at_end"] <= 2:
            out["squads_left_at_end"] = 1
            out["won_guess"] = True

    with open(folder / "path.json", "w") as f:
        json.dump(path, f)
    if path:
        draw_path(locator, path, landing, death, folder / "path.jpg")
    if not (folder / ".stitched").exists():          # each match goes into the stitched map once
        locator.stitch(sightings)
        (folder / ".stitched").touch()
    try:
        if path:                                  # nothing placed (map picture doesn't match): keep the last picture
            out["match_map"] = draw_match_map(folder, stats, events, locator, found, path, ship, landing, death, died_at)
            out["players_out"] = out["match_map"].pop("players_out", [])
    except Exception as e:
        out["match_map_error"] = f"{e.__class__.__name__}: {e}"
    return out

DOWN_GAP = 45                  # s; your trail stopping this long mid-match then carrying on = died and came back
RING_WINDOW = 20               # s of minimaps pooled for one ring fit
RING_MAX_RADIUS = 0.45         # share of the map's width; bigger rings come from map screenshots
RING_MIN_ARC = 60              # degrees of the ring's edge that must have been seen
RING_MIN_RADIUS = 0.04         # share of the map's width; smaller "rings" are red map details
RING_MIN_FRAMES = 5            # minimaps that must show the edge in one window

def minimap_ring_edge(minimap, fit):
    """Points (on the reference) along the edge of the red "outside the ring" tint on one minimap."""
    m = MINIMAP_MARGIN
    crop = minimap[m:-m, m:-m]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    red = (((hsv[..., 0] < 8) | (hsv[..., 0] > 172)) & (hsv[..., 1] > 90) & (hsv[..., 2] > 35)).astype(np.uint8)
    kernel = np.ones((7, 7), np.uint8)
    red = cv2.morphologyEx(cv2.morphologyEx(red, cv2.MORPH_CLOSE, kernel), cv2.MORPH_OPEN, kernel)  # not pings
    # The tint is one big area running off the edge of the minimap; red icons / markers are small blobs
    n, labels, stats, _ = cv2.connectedComponentsWithStats(red)
    if n <= 1:
        return np.empty((0, 2))
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[biggest, cv2.CC_STAT_AREA] < 0.05 * red.size:
        return np.empty((0, 2))
    tint = (labels == biggest).astype(np.uint8)
    # Icons sitting on the tint punch holes in it: fill them, only the outline is wanted
    contours, _ = cv2.findContours(tint, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    outline = max(contours, key=cv2.contourArea)[:, 0, :]
    h, w = tint.shape
    keep = (outline[:, 0] > 4) & (outline[:, 0] < w - 5) & (outline[:, 1] > 4) & (outline[:, 1] < h - 5)
    # Map icons on the ring's edge (respawn beacons, Replicators...) dent the outline: not the ring's edge there
    import Map_Icons
    for _, x, y, size, _ in Map_Icons.find_icons(crop, Map_Icons.SCALES_MINIMAP, Map_Icons.CLEAN_SCORE):
        r = size * 0.7
        keep &= (np.abs(outline[:, 0] - x) > r) | (np.abs(outline[:, 1] - y) > r)
    pts = outline[keep].astype(np.float64)
    if len(pts) < 20 or not 0.03 < tint.mean() < 0.97:
        return np.empty((0, 2))
    pts = np.column_stack([pts, np.ones(len(pts))])[:: max(1, len(pts) // 150)]
    return pts @ fit.T

def ring_prior(rings, when=None):
    """What the rings seen so far (consistent_rings' output) say now: (ring, next ring), each (x, y, r) or None -
    the smallest ring (rings only shrink) and the smallest next ring inside it. `when`: only rings seen by then.
    Only sure rings count: seen on two map shots at different times, or a minimap run (already several sightings) -
    a one-off (a red marker circle fitted as a ring) taken as the prior would hide every real ring after it."""
    seen = [r for r in rings if not when or r.get("time", "") <= when]
    same = lambda a, b: abs(a["r"] - b["r"]) <= 0.15 * b["r"] + 3 and np.hypot(a["x"] - b["x"], a["y"] - b["y"]) <= 0.15 * b["r"] + 3
    seen = [r for r in seen if r.get("source") == "minimap" or any(
        o is not r and o["kind"] == r["kind"] and o.get("time") != r.get("time") and same(o, r) for o in seen)]
    ring = min((r for r in seen if r["kind"] == "ring"), key=lambda r: r["r"], default=None)
    nxt = min((r for r in seen if r["kind"] == "next ring" and (not ring or r["r"] < ring["r"] * 0.95)),
              key=lambda r: r["r"], default=None)
    return tuple((c["x"], c["y"], c["r"]) if c else None for c in (ring, nxt))

def inside_ring(circle, ring):
    """Is a circle (x, y, r) inside the ring (x, y, r) - every ring is inside the one before (a little slack:
    measured centres wobble)."""
    return not ring or np.hypot(circle[0] - ring[0], circle[1] - ring[1]) + circle[2] <= ring[2] * 1.05 + 5

def minimap_ring(minimap, fit, sizes=(), prior=None):
    """The ring as seen on one minimap, on the reference: (x, y, r) or None; (x, y, r, True) when it's exactly
    the next ring already known (it has closed). Late in a match the whole ring fits on the minimap as an
    untinted hole in the red / pink tint; earlier only an arc of it shows, which is fitted with this map's known
    ring sizes. `prior`: ring_prior() - the ring found has to be inside the ring known so far."""
    import Match_Map
    m = MINIMAP_MARGIN
    ring, nxt = prior or (None, None)
    edge = minimap_ring_edge(minimap, fit)
    # The next ring is known (map / grey disc): once the tint's edge runs along exactly that circle it has
    # closed - while it's closing the edge is bigger and off-centre - so this needs no waiting to be sure
    if nxt and len(edge) >= 30:
        off = np.abs(np.hypot(edge[:, 0] - nxt[0], edge[:, 1] - nxt[1]) - nxt[2])
        if (off <= 2.5 + 0.02 * nxt[2]).mean() >= MINIMAP_ARC_FIT:
            return (nxt[0], nxt[1], nxt[2], True)
    if ring:
        sizes = [s for s in sizes if s <= ring[2] * 1.05]      # never bigger than the ring before
    circle = Match_Map.ring_from_hole(minimap[m:-m, m:-m])
    if circle:
        x, y = fit @ np.array([circle[0], circle[1], 1.0])
        r = float(circle[2] * np.hypot(fit[0, 0], fit[1, 0]))
        # Measured freely the radius wobbles ~10% minimap to minimap, too much for the sightings to agree;
        # near a known ring size, fitting that exact size to the tint's edge holds steady
        near = [s for s in sizes if abs(r - s) <= MINIMAP_SNAP * s]
        snapped = Match_Map.fit_known_circle(edge, near[0], tolerance=2.5) if near and len(edge) >= 30 else None
        if snapped and snapped[3] >= 0.4 * len(edge):
            return (snapped[0], snapped[1], snapped[2]) if inside_ring(snapped, ring) else None
        return (float(x), float(y), r) if inside_ring((x, y, r), ring) else None
    if len(edge) < 30:
        return None
    # The tint's edge is still the ring already known (it hasn't closed yet): nothing new. A short, nearly
    # straight arc of it fits a smaller size too, bent the other way (2026-10-30 15:34: the 300 px ring's
    # edge fitted 147 px circles 150 px off, on 5 minimaps)
    if ring:
        off = np.abs(np.hypot(edge[:, 0] - ring[0], edge[:, 1] - ring[1]) - ring[2])
        if (off <= 2.5 + 0.02 * ring[2]).mean() >= MINIMAP_ARC_FIT:
            return None
    fits = [c for c in (Match_Map.fit_known_circle(edge, size, tolerance=2.5) for size in sizes)
            if c and c[3] >= MINIMAP_ARC_FIT * len(edge) and inside_ring(c, ring)]
    if not fits:
        return None
    fits.sort(key=lambda c: -c[3])
    # Two sizes fitting about as well: the arc's too short to tell its curve, so it says nothing
    if len(fits) > 1 and fits[1][3] >= MINIMAP_ARC_AMBIGUOUS * fits[0][3]:
        return None
    best = fits[0]
    return (best[0], best[1], best[2])

def minimap_next_ring(minimap, fit):
    """The next ring as the lighter grey disc on the minimap (shown once a ring console has revealed it,
    or once it's close), on the reference: (x, y, r) or None."""
    import Match_Map
    m = MINIMAP_MARGIN
    disc = Match_Map.brighter_disc(minimap[m:-m, m:-m])
    if not disc:
        return None
    x, y = fit @ np.array([disc[0], disc[1], 1.0])
    return (float(x), float(y), float(disc[2] * np.hypot(fit[0, 0], fit[1, 0])))

MINIMAP_SNAP = 0.15            # a minimap ring this close to a known ring size is fitted at that size
MINIMAP_ARC_FIT = 0.7          # share of the tint's edge a known ring size must run along (the rest: dents, icons)
MINIMAP_ARC_AMBIGUOUS = 0.9    # a second size fitting this much as well as the best: arc too short to choose
RING_HOLD = 3                  # s the same circle must keep showing on the minimap to count as a ring (3+
                               # sightings): rings hold still for a minute+, a closing ring keeps shrinking. Only
                               # known ring sizes inside the ring before are tried, so this can be short (25 s made
                               # the minimap ~15 s slower than opening the map; 2-3 s showed no wrong rings on 3
                               # test matches). The known next ring, seen exactly, needs 2 sightings and no wait

def rings_from_minimaps(found, map_width, sizes=(), known=()):
    """Rings that held still, from the minimaps: runs of minimaps showing the same circle. `known`: rings
    already found (map screens) - each minimap's ring has to fit inside the ring known by then."""
    samples = [(datetime.fromisoformat(e["time"]), minimap_ring(minimap, w["fit"], sizes, ring_prior(known, e["time"])))
               for e, w, minimap in found]
    return rings_from_samples(samples, map_width, sizes)

def rings_from_samples(samples, map_width, sizes=(), kind="ring"):
    """`samples`: [(time, (x, y, r) or None)] from minimaps, oldest first. A ring holding still shows the
    same circle minimap after minimap (give or take the measuring wobble); a closing one keeps shrinking,
    so its sightings never agree for long."""
    circles = [(t, c) for t, c in samples if c and 0.012 * map_width <= c[2] <= map_width]   # final rings are tiny
    runs = []                                      # [[(time, circle), ...], ...]
    def middle(run):
        return [float(np.median([c[i] for _, c in run])) for i in range(3)]
    for t, c in circles:
        for run in reversed(runs):
            mx, my, mr = middle(run)
            if ((t - run[-1][0]).total_seconds() <= 15 and abs(c[2] - mr) <= 0.08 * mr + 3
                    and np.hypot(c[0] - mx, c[1] - my) <= 0.08 * mr + 3):
                run.append((t, c))
                break
        else:
            runs.append([(t, c)])
    out = []
    for run in runs:
        radii = [c[2] for _, c in run]
        lasting = (run[-1][0] - run[0][0]).total_seconds()
        # Steady: the first and last thirds of the run measure the same size (a closing ring doesn't)
        third = max(1, len(radii) // 3)
        steady = abs(np.median(radii[:third]) - np.median(radii[-third:])) <= 0.03 * np.median(radii) + 2
        exact = sum(len(c) > 3 for _, c in run) >= 2        # the known next ring, seen exactly (minimap_ring)
        if exact or (len(run) >= 3 and lasting >= RING_HOLD and steady):
            x, y, r = middle(run)
            out.append({"kind": kind, "x": x, "y": y, "r": r, "source": "minimap",
                        "time": run[-1][0].isoformat(timespec="seconds")})
    return out

def ring_phase(screenshot):
    """The ring timer on the full-map screen (left panel): {"closing": bool, "round": n or None}."""
    from Extract_Stats import read_text, squash
    s = screenshot.shape[1] / 1920
    text = " ".join(t for _, _, t in read_text(screenshot[int(300 * s):int(760 * s), :int(455 * s)]))
    closing = "CLOSING" in squash(text)
    m = re.search(r"ROUND\s*(\d)", text.upper()) or re.search(r"(?:^|\s)(\d)\s+\d{1,2}:\d{2}", text)
    return {"closing": closing, "round": int(m.group(1)) if m else None}

def teammates_who_left(folder, events, named, ended=None):
    """{colour: when last seen} for teammates who quit: their name dropped off the squad panel for good
    (a dead teammate stays on it). Read from the squad panel crops (and the full screenshots, which have
    it too) from the end backwards, until everyone named has been found once."""
    import Match_Map
    from Extract_Stats import is_me
    shots = [e for e in events if e.get("file") and e["screen"] in ("in_game", "spectate")
             and e["event"] in ("squad", "full") and (not ended or datetime.fromisoformat(e["time"]) <= ended)]
    last_seen, reads_since = {}, {c: 0 for c in named}
    for e in sorted(shots, key=lambda e: e["time"], reverse=True):
        if len(last_seen) == len(named):
            break
        image = cv2.imread(str(folder / e["file"]))
        if image is None:
            continue
        panel, s = Match_Map.squad_panel_of(image) if e["event"] == "full" else \
            (image, image.shape[1] / (Match_Map.SQUAD_PANEL[2] - Match_Map.SQUAD_PANEL[0]))
        names = Match_Map.read_squad_panel(panel, s)[0]
        if not any(is_me(n) for n in names):
            continue                              # couldn't read the panel this time
        for colour, name in named.items():
            if colour in last_seen:
                continue
            if Match_Map.on_panel(name, names):
                last_seen[colour] = datetime.fromisoformat(e["time"])
            else:
                reads_since[colour] += 1
    return {c: when for c, when in last_seen.items() if reads_since[c] >= Match_Map.LEFT_AFTER}

def draw_match_map(folder, stats, events, locator, found, path, ship, landing, death, died_at):
    """The match picture: everyone's trail, the dropship, the ring. Also becomes "Latest Match Map.png"."""
    import Match_Map
    from Extract_Stats import is_me
    # The squad panel (names in their colours) is on the dropship screenshot, and on map screenshots too
    colours = {}
    for e in events:
        if e["screen"] in ("dropship", "map") and e.get("file") and len(colours) < 3:
            for colour, name in Match_Map.squad_colours(cv2.imread(str(folder / e["file"]))).items():
                colours.setdefault(colour, name)
    # One name per colour: a name the panel read twice can't tell which colour it is
    names_seen = list(colours.values())
    colours = {c: n for c, n in colours.items() if names_seen.count(n) == 1}
    # Your colour: the arrow in the middle of the minimap while you're alive is the surest sign; the
    # squad panel can mislabel you (it's read from screenshots that may be taken while you're down)
    alive = [m for e, _, m in found if e["screen"] == "in_game"][:40]
    mine = (Match_Map.my_colour(alive) if len(alive) >= 5 else None) or next((c for c, n in colours.items() if is_me(n)), None)
    if mine and colours.get(mine) and not is_me(colours[mine]):
        colours = {c: n for c, n in colours.items() if not is_me(n)}       # the panel had it the wrong way round
    colours = {c: n for c, n in colours.items() if c != mine or is_me(n)}
    # Who was actually in the squad: the end card, else legend select. Panel names that don't match
    # anyone there are misreads ("AST"), and with no teammates there's nobody to follow
    from Extract_Stats import name_similarity
    cards = (stats.get("summary") or {}).get("players") or stats.get("legend_select", {}).get("squad", [])
    squad = [p.get("name") for p in cards if p.get("name") and not is_me(p.get("name"))]
    # Someone who quit isn't on the end card, but they picked a legend (and the panel had their name): still
    # a teammate, up to when they left
    for p in stats.get("legend_select", {}).get("squad", []):
        n = p.get("name")
        if n and not is_me(n) and not any(name_similarity(n, q) >= 0.7 for q in squad) \
                and any(name_similarity(n, c) >= 0.7 for c in colours.values()):
            squad.append(n)
    colours = {c: n for c, n in colours.items()
               if c == mine or any(name_similarity(n, q) >= 0.7 for q in squad)}
    others = [c for c in Match_Map.SQUAD_COLOURS if c != mine]
    if cards:
        named = [c for c in others if c in colours]
        mates = named if len(named) >= len(squad) else (others if len(squad) >= 2 else named or others)
        mates = mates if squad else []
    else:
        mates = others
    tracker = Match_Map.TeammateTracker(mates, MINIMAP_MARGIN, locator.reference)
    ended = datetime.fromisoformat(stats["ended"]) if stats.get("ended") else None
    for e, where, minimap in found:
        when = datetime.fromisoformat(e["time"])
        if e["screen"] not in ("in_game", "spectate") or (ended and when > ended):
            continue                              # dropship (everyone in one spot) / after the squad was out
        if e["screen"] == "spectate" or (died_at and when > died_at):
            # Dead: the minimap follows whoever you're spectating, so it's their trail now
            h = minimap.shape[0] // 2
            watched = Match_Map.colour_of(cv2.cvtColor(minimap[h - 10:h + 10, h - 10:h + 10], cv2.COLOR_BGR2HSV))
            if watched in mates:
                tracker.add_point(watched, {"when": when, "x": where["x"], "y": where["y"], "watched": True})
            continue
        tracker.add(minimap, where["fit"], when, (where["x"], where["y"]))
    # A teammate who quit: after that, anything their colour on the minimap is the map's own art
    left = teammates_who_left(folder, events, {c: colours[c] for c in mates if c in colours}, ended)
    for colour, when in left.items():
        tracker.quit(colour, when)
    if squad and len(mates) > len(squad):
        # Followed every colour because the panel didn't say whose is whose: only as many teammates as the
        # squad had, the colours seen most (the rest is map icons of that colour)
        mates = sorted(mates, key=lambda c: len(tracker.trails[c]), reverse=True)[:len(squad)]
        tracker.trails = {c: t for c, t in tracker.trails.items() if c in mates}
        if len(squad) == 1 and tracker.trails[mates[0]]:
            colours.setdefault(mates[0], squad[0])           # one teammate: that trail is them
    rings, screenshots, scans, map_spots, map_shots, flights_seen = [], [], [], [], [], []
    jumped = next((x["time"] for x in events if x["screen"] == "in_game" and x["event"] == "enter"), None)
    for e in events:
        # The map screen, plus unrecognised screens that may be the map opened while dead
        if (e["screen"] == "map" or e["event"] == "after_match") and e.get("file"):
            shot = cv2.imread(str(folder / e["file"]))
            if shot is None:
                continue
            screenshots.append((f"{folder.name}_{Path(e['file']).stem}", shot, bool(jumped) and e["time"] <= jumped))
            # Opened in the dropship / just after the jump: the dropship's real flight line is on it
            if len(flights_seen) < 3 and (not jumped or (datetime.fromisoformat(e["time"]) - datetime.fromisoformat(jumped)
                                                          ).total_seconds() <= Match_Map.FLIGHT_SHOT_AFTER):
                seen = Match_Map.flight_from_map_screen(locator, shot)
                flights_seen += [seen] if seen else []
            scans += [dict(sc, time=e["time"]) for sc in Match_Map.scans_from_map_screen(locator, shot)]
            if not ended or e["time"] <= stats["ended"]:     # after that, the arrows are another squad's
                map_spots.append((datetime.fromisoformat(e["time"]), Match_Map.squad_on_map_screen(locator, shot)))
                map_shots.append((e["time"], shot))
            # Every ring is inside the one before: what's been found so far narrows down where to look
            prior = ring_prior(Match_Map.consistent_rings(rings, locator.reference.shape[1::-1], locator.ring_sizes))
            found_rings = Match_Map.rings_from_map_screen(locator, shot, prior)
            phase = ring_phase(shot) if found_rings else {}
            for ring in found_rings:
                # Caught while closing: the red edge is just where the ring had got to, not a stage (and where
                # it's going is the next ring)
                if ring["kind"] == "ring" and phase.get("closing"):
                    continue
                rings.append(dict(ring, time=e["time"], round=phase.get("round")))
    add_map_screens(locator, screenshots)          # helps clean up the map picture for next time
    # Care Packages, Evo Harvesters, Replicators... seen on the minimaps and the map: the SVG's items layer
    import Map_Icons
    items = Map_Icons.match_items(found, map_shots, locator, MINIMAP_MARGIN, stats.get("ended"))
    # Rings you didn't open the map for: from the ring's edge on the minimaps
    # (spectating a teammate counts: their minimap shows the same ring)
    # (and watching another squad after yours was out: the ring is the same for everyone)
    match_over = datetime.fromisoformat(stats["ended"]) if stats.get("ended") else None
    in_match = [f for f in found if f[0]["screen"] == "after_squad" or (f[0]["screen"] in ("in_game", "spectate")
                and (not match_over or datetime.fromisoformat(f[0]["time"]) <= match_over))]
    # The next ring, wherever the minimap showed it as a steady lighter disc
    rings += rings_from_samples([(datetime.fromisoformat(e["time"]), minimap_next_ring(m, w["fit"])) for e, w, m in in_match],
                                locator.reference.shape[1], kind="next ring")
    known = Match_Map.consistent_rings(rings, locator.reference.shape[1::-1], locator.ring_sizes)
    rings += rings_from_minimaps(in_match, locator.reference.shape[1], locator.ring_sizes, known)
    rings = Match_Map.consistent_rings(rings, locator.reference.shape[1::-1], locator.ring_sizes)
    from Extract_Stats import RESPAWN_MODES
    if stats.get("mode") not in RESPAWN_MODES:
        locator.learn_ring_sizes(rings, folder.name)
    me_trail = [p for p in path if p["screen"] == "in_game" and (not died_at or datetime.fromisoformat(p["time"]) <= died_at)]
    for a, b in zip(me_trail, me_trail[1:]):
        if not steady_move(a, b):
            b["gap"] = True                        # respawned elsewhere (the jump itself may have been while spectating)
    summary = stats.get("summary") or {}
    mine_card = next((p for p in summary.get("players", []) if is_me(p.get("name"))), {})
    my_pick = next((p for p in stats.get("legend_select", {}).get("squad", []) if is_me(p.get("name"))), {})
    ended = datetime.fromisoformat(stats["ended"]) if stats.get("ended") else None
    bits = [stats.get("mode", ""), locator.map_name,
            f"#{summary['placement']}" if summary.get("placement") else "",
            my_pick.get("legend", ""),
            f"{mine_card['kills']} kill{'' if mine_card['kills'] == 1 else 's'}" if mine_card.get("kills") is not None else "",
            f"{mine_card['damage']} dmg" if mine_card.get("damage") is not None else "",
            f"{ended:%b %d %H:%M}" if ended else ""]
    # Legend lines: you first, then whoever has a trail or a name
    names = {mine: f"{colours[mine]} (you)" if mine in colours else "You"} if mine else {}
    names.update({c: colours.get(c, f"Teammate ({c})") for c in mates if tracker.trails[c] or c in colours})
    # Died and came back (respawn beacon / recovery): where your trail stopped, and where it picked up
    downs = []
    for a, b in zip(me_trail, me_trail[1:]):
        gone = (datetime.fromisoformat(b["time"]) - datetime.fromisoformat(a["time"])).total_seconds()
        if gone >= DOWN_GAP:
            downs.append({"down": {"time": a["time"], "x": a["x"], "y": a["y"]},
                          "back": {"time": b["time"], "x": b["x"], "y": b["y"]}})
    # The dropship's whole flight: compass heading through where it was seen / where you jumped
    # (only if the match was seen from the start: joining part-way, the first spot isn't the jump)
    from_start = any(e["screen"] in ("legend_select", "your_squad", "dropship") for e in events)
    jump = next((p for p in path if p["screen"] == "in_game"), None) if from_start else None
    heading = (stats.get("dropship") or {}).get("heading")
    moved = heading_of(ship)
    if heading is not None and moved is not None and (stats.get("dropship") or {}).get("heading_from"):
        # The heading came from after the jump (you may have turned): trust the dropship's own movement
        if min(abs(heading - moved), 360 - abs(heading - moved)) > 30:
            heading = moved
            stats.setdefault("dropship", {})["heading"] = moved
    # (a full-map shot's flight line that agrees with that heading is the real path: it says where it ran)
    flight = Match_Map.best_flight(locator.reference.shape[1::-1], heading=heading, ship=ship, jump=jump, seen=flights_seen)
    if stats.get("dropzone"):
        flight = None                   # Ranked - Dropzone: squads spawn on the ground, there's no flight to draw
    out_file = folder / "match_map.png"
    scans = Match_Map.distinct_scans(scans)
    # While the map was open there's no minimap: everyone's position from the map itself fills that gap
    if mine:
        Match_Map.fill_from_map_screens(me_trail, [(t, s[mine]) for t, s in map_spots if mine in s and (not died_at or t <= died_at)], "time")
    for colour, trail in tracker.trails.items():
        if trail:
            Match_Map.fill_from_map_screens(trail, [(t, s[colour]) for t, s in map_spots if colour in s], "when")
    Match_Map.render_both(locator, out_file, folder / "match_map_zoom.png", me_trail, downs=downs, scans=scans, mates={c: t for c, t in tracker.trails.items() if t},
                     ship=flight, rings=rings, landing=landing, death=death, header="  \u00b7  ".join(b for b in bits if b),
                     my_colour_name=mine, names=names)
    # The same match as vector layers + plain data, for zooming right in and for a UI later
    from urllib.parse import quote
    import Map_Icons
    href = quote(os.path.relpath(Map_Icons.drawing_reference(locator.folder), folder))     # the map without its icons
    when = lambda p: Match_Map.point_time(p).isoformat(timespec="seconds")
    players = [{"name": names.get(c, c).replace(" (you)", "") if c else "You", "colour": c or "", "is_me": c == mine,
                "points": [dict(time=when(p), x=round(p["x"], 1), y=round(p["y"], 1),
                                **{k: True for k in ("gap", "watched") if p.get(k)})
                           for p in (me_trail if c == mine else Match_Map.smooth(tracker.trails[c]))]}
               for c in [mine] + [c for c in mates if tracker.trails[c]] if c]
    keep = lambda p: {k: v for k, v in p.items() if k in ("time", "x", "y", "poi", "zone")} if p else None
    Match_Map.write_svg(locator, folder / "match_paths.svg", players, rings=rings, ship=flight, landing=landing,
                        death=death, downs=downs, scans=scans, items=items, title=" \u00b7 ".join(b for b in bits if b), map_href=href)
    with open(folder / "trails.json", "w") as f:
        json.dump({"map": locator.map_name, "mode": stats.get("mode", ""), "reference": href, "reference_size": list(locator.reference.shape[1::-1]),
                   "title": " \u00b7 ".join(b for b in bits if b), "players": players,
                   "rings": Match_Map.distinct_rings(rings), "dropship": flight,
                   "landing": keep(landing), "death": keep(death), "downs": downs, "scans": scans, "items": items,
                   "pois": locator.pois}, f)
    Match_Map.publish(out_file)
    # Where each player was last seen (for teammates: where they went down for good)
    out_zones = [{"name": pl["name"], "colour": pl["colour"], "is_me": pl["is_me"], "time": pl["points"][-1]["time"],
                  "zone": locator.nearest_poi(pl["points"][-1]["x"], pl["points"][-1]["y"])}
                 for pl in players if pl["points"]]
    return {"file": out_file.name, "me": mine, "squad_colours": colours, "rings": rings, "scans": scans, "items": items,
            "players_out": out_zones,
            "teammate_points": {c: len(t) for c, t in tracker.trails.items()},
            "teammates_left": {colours.get(c, c): when.isoformat(timespec="seconds") for c, when in left.items()}}

# ---------------------------
# MAIN SCRIPT
# ---------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("match", nargs="?", help="a Captures/<match> folder to work out")
    parser.add_argument("--build-assets", action="store_true", help="map pictures + POIs out of the workbook")
    parser.add_argument("--remove-images", action="store_true", help="with --build-assets: also take the pictures out of the workbook")
    parser.add_argument("--reference-from", metavar="SCREENSHOT", help="make a map picture from a zoomed-out full-map screenshot")
    parser.add_argument("--map", choices=MAP_NAMES, help="which map --reference-from is")
    args = parser.parse_args()
    if args.reference_from:
        if not args.map:
            parser.error("--reference-from needs --map")
        reference_from(args.reference_from, args.map)
    elif args.build_assets:
        build_assets(remove_images=args.remove_images)
    elif args.match:
        folder = Path(args.match)
        stats = json.loads((folder / "stats.json").read_text())
        print(json.dumps(analyse_match(folder, stats), indent=2))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
