"""
All your games on one map: where players go, and where the rings went.

For every map, from the trails.json of every match played on it:
    Maps/<Map>/heatmap.png   how many player-trips passed through each spot (you and teammates, after
                             landing). A player standing still counts once, so busy routes, rotations and
                             choke points light up, not just camping spots. Landings are green dots.
    Maps/<Map>/rings.png     every ring recorded (faint), the last ring seen each game (bold) and its
                             centre, so you can see where endgames tend to end up.

Usage:
    python Heatmap.py               # every map
    python Heatmap.py "Broken Moon"  # one map
    python Heatmap.py --me          # only your own trails
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import Map_Locate
import Match_Map

HERE = Path(__file__).parent
CAPTURES = HERE / "Captures"
TRAIL_WIDTH = 0.004            # share of the map width a trail covers
BLUR = 0.004                   # share of the map width the heat is smoothed over
TRAIL_GAP = Match_Map.TRAIL_GAP

def load_matches(map_name):
    out = []
    for path in sorted(CAPTURES.glob("*/trails.json")):
        data = json.loads(path.read_text())
        if data.get("map") == map_name:
            out.append(data)
    return out

def trail_mask(points, landed_at, shape, width):
    """Where one player walked in one match (after landing; no lines across gaps), as a 0/1 mask."""
    mask = np.zeros(shape, np.uint8)
    times = [datetime.fromisoformat(p["time"]) for p in points]
    for (a, ta), (b, tb) in zip(zip(points, times), zip(points[1:], times[1:])):
        if b.get("gap") or (tb - ta).total_seconds() > TRAIL_GAP or (landed_at and tb <= landed_at):
            continue
        cv2.line(mask, (int(a["x"]), int(a["y"])), (int(b["x"]), int(b["y"])), 1, width, cv2.LINE_AA)
    return mask

def colour_heat(heat):
    """Heat (0-1) -> BGR colour and alpha: blue (rare) through yellow to red (busiest)."""
    lut = cv2.applyColorMap(np.uint8(np.clip(heat, 0, 1) * 255), cv2.COLORMAP_TURBO)
    alpha = np.clip(heat * 3, 0, 0.85)[..., None]
    return lut, alpha

def labels(image, locator, scale, title, lines=()):
    from PIL import Image, ImageDraw
    pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    small, big = Match_Map.font(18), Match_Map.font(26)
    for poi in locator.pois:
        draw.text((poi["x"] * scale, poi["y"] * scale), poi["name"].upper(), font=small, fill=(235, 235, 235),
                  anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))
    draw.text((16, 14), title, font=big, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    for i, line in enumerate(lines):
        draw.text((16, 50 + 24 * i), line, font=small, fill=(220, 220, 220), stroke_width=3, stroke_fill=(0, 0, 0))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

def heatmap(map_name, only_me=False):
    locator = Map_Locate.MapLocator(map_name)
    matches = load_matches(map_name)
    if not matches:
        return None
    h, w = locator.reference.shape[:2]
    width = max(2, int(TRAIL_WIDTH * w))
    heat = np.zeros((h, w), np.float32)
    landings, trips = [], 0
    for data in matches:
        landed_at = datetime.fromisoformat(data["landing"]["time"]) if data.get("landing") else None
        if data.get("landing"):
            landings.append(data["landing"])
        for player in data["players"]:
            if only_me and not player.get("is_me"):
                continue
            mask = trail_mask(player["points"], landed_at, (h, w), width)
            if mask.any():
                heat += mask
                trips += 1
    if trips == 0:
        return None
    heat = cv2.GaussianBlur(heat, (0, 0), max(1.0, BLUR * w))
    top = np.percentile(heat[heat > 0], 99) if (heat > 0).any() else 1
    norm = heat / max(top, 1e-6)

    scale = Match_Map.OUTPUT_SIZE / max(h, w)
    size = (int(w * scale), int(h * scale))
    base = cv2.resize(locator.reference, size, interpolation=cv2.INTER_AREA).astype(np.float32) * 0.45
    colour, alpha = colour_heat(cv2.resize(norm, size, interpolation=cv2.INTER_LINEAR))
    image = (base * (1 - alpha) + colour.astype(np.float32) * alpha).astype(np.uint8)
    for land in landings:
        cv2.circle(image, (int(land["x"] * scale), int(land["y"] * scale)), 6, (80, 255, 80), -1, cv2.LINE_AA)
        cv2.circle(image, (int(land["x"] * scale), int(land["y"] * scale)), 6, (0, 0, 0), 1, cv2.LINE_AA)
    who = "your trails" if only_me else "squad trails"
    image = labels(image, locator, scale, f"{map_name} - where players go",
                   [f"{len(matches)} games, {trips} {who}", "blue = sometimes, red = most often", "green dots = landings"])
    out = locator.folder / ("heatmap_me.png" if only_me else "heatmap.png")
    cv2.imwrite(str(out), image)
    return out

def ring_history(map_name):
    locator = Map_Locate.MapLocator(map_name)
    matches = load_matches(map_name)
    rings = [(data, [r for r in data.get("rings", []) if r["kind"] == "ring"]) for data in matches]
    rings = [(d, rs) for d, rs in rings if rs]
    if not rings:
        return None
    h, w = locator.reference.shape[:2]
    scale = Match_Map.OUTPUT_SIZE / max(h, w)
    size = (int(w * scale), int(h * scale))
    image = (cv2.resize(locator.reference, size, interpolation=cv2.INTER_AREA).astype(np.float32) * 0.55).astype(np.uint8)
    for _, rs in rings:
        for r in rs:
            cv2.circle(image, (int(r["x"] * scale), int(r["y"] * scale)), int(r["r"] * scale), (200, 200, 200), 1, cv2.LINE_AA)
    for _, rs in rings:
        last = min(rs, key=lambda r: r["r"])
        c = (int(last["x"] * scale), int(last["y"] * scale))
        cv2.circle(image, c, int(last["r"] * scale), (60, 200, 255), 2, cv2.LINE_AA)
        cv2.circle(image, c, 5, (60, 200, 255), -1, cv2.LINE_AA)
    image = labels(image, locator, scale, f"{map_name} - where the rings went",
                   [f"{len(rings)} games with rings", "grey = every ring seen", "orange = smallest ring seen each game"])
    out = locator.folder / "rings.png"
    cv2.imwrite(str(out), image)
    return out

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("map", nargs="?", choices=Map_Locate.MAP_NAMES)
    parser.add_argument("--me", action="store_true", help="only your own trails")
    args = parser.parse_args()
    for name in [args.map] if args.map else Map_Locate.MAP_NAMES:
        made = [heatmap(name, args.me), ring_history(name)]
        made = [str(m) for m in made if m]
        if made:
            print(f"{name}: " + ", ".join(made))

if __name__ == "__main__":
    main()
