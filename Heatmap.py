"""
All your games on one map: where players go, and where the rings went.

For every map, from the trails.json of every match played on it:
    Maps/<Map>/heatmap.png   how many player-trips passed through each spot (you and teammates, after
                             landing). A player standing still counts once, so busy routes, rotations and
                             choke points light up, not just camping spots. Landings are green dots.
    Maps/<Map>/rings.png     where endgames end up: the smallest rings seen (a bigger ring is hidden once
                             3+ games had a smaller ring inside it; only the last 3 ring sizes), the same
                             spot in different games merged into one circle with a count. Also end_zones.json.

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

COVERED_BY = 3                 # a ring is hidden once this many games had a smaller ring inside it
SMALLEST_STAGES = 3            # only the last few ring stages (sizes) are shown
SAME_SPOT = 0.9                # circles overlapping this much (area) count as the same end zone

def overlap(a, b):
    """Shared area of two circles as a share of the bigger one (1 = the same circle)."""
    r1, r2 = a["r"], b["r"]
    d = np.hypot(a["x"] - b["x"], a["y"] - b["y"])
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        shared = np.pi * min(r1, r2) ** 2
    else:
        p1 = r1 ** 2 * np.arccos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
        p2 = r2 ** 2 * np.arccos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2))
        p3 = 0.5 * np.sqrt(max(0.0, (-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2)))
        shared = p1 + p2 - p3
    return shared / (np.pi * max(r1, r2) ** 2)

def merge(rings, same):
    groups = []
    for r in sorted(rings, key=lambda r: r["r"]):
        for g in groups:
            if same(g[0], r):
                g.append(r)
                break
        else:
            groups.append([r])
    return groups

def inside(small, big):
    return small["r"] < 0.9 * big["r"] and np.hypot(small["x"] - big["x"], small["y"] - big["y"]) + small["r"] <= big["r"] * 1.05 + 3

def end_zones(matches, map_width=None, sizes=()):
    """The likely end zones on a map: the smallest rings seen, merged where they're the same spot.
    `sizes`: the map's known ring sizes (rings caught mid-close in older games are left out).
    Returns [{"x", "y", "r", "games"}] smallest first."""
    rings = []
    for i, data in enumerate(matches):
        # One game sees the same ring several times (map, minimap): one copy each
        own = [r for r in data.get("rings", []) if r["kind"] == "ring" and (not map_width or r["r"] >= 0.015 * map_width)
               and Match_Map.ring_stage_check(r, sizes)]
        for g in merge(own, lambda a, b: overlap(a, b) >= 0.8):
            rings.append({"game": i, **{k: float(np.median([m[k] for m in g])) for k in ("x", "y", "r")}})
    # A ring that several games narrowed down from is no longer interesting: show what's inside it
    rings = [r for r in rings if len({o["game"] for o in rings if inside(o, r)}) < COVERED_BY]
    # Only the last few stages (ring sizes)
    tiers = []
    for radius in sorted(r["r"] for r in rings):
        if not tiers or radius > tiers[-1] * 1.12:
            tiers.append(radius)
    if len(tiers) > SMALLEST_STAGES:
        limit = tiers[SMALLEST_STAGES] / 1.12
        rings = [r for r in rings if r["r"] < limit]
    # Same spot and size in different games: one circle, counted
    out = []
    for ms in merge(rings, lambda a, b: overlap(a, b) >= SAME_SPOT):
        out.append({"x": float(np.median([m["x"] for m in ms])), "y": float(np.median([m["y"] for m in ms])),
                    "r": float(np.median([m["r"] for m in ms])), "games": len({m["game"] for m in ms})})
    return sorted(out, key=lambda z: z["r"])

def ring_history(map_name):
    locator = Map_Locate.MapLocator(map_name)
    matches = [d for d in load_matches(map_name) if any(r["kind"] == "ring" for r in d.get("rings", []))]
    if not matches:
        return None
    h, w = locator.reference.shape[:2]
    zones = end_zones(matches, w, locator.ring_sizes)
    if not zones:
        return None
    scale = Match_Map.OUTPUT_SIZE / max(h, w)
    size = (int(w * scale), int(h * scale))
    image = (cv2.resize(locator.reference, size, interpolation=cv2.INTER_AREA).astype(np.float32) * 0.55).astype(np.uint8)
    top = max(z["games"] for z in zones)
    for z in sorted(zones, key=lambda z: -z["r"]):        # big first, so small ones sit on top
        c, r = (int(z["x"] * scale), int(z["y"] * scale)), int(z["r"] * scale)
        strength = z["games"] / top
        overlay = image.copy()
        cv2.circle(overlay, c, r, (60, 200, 255), -1, cv2.LINE_AA)
        image = cv2.addWeighted(overlay, 0.08 + 0.17 * strength, image, 1 - (0.08 + 0.17 * strength), 0)
        cv2.circle(image, c, r, (60, 200, 255), 1 + int(2 * strength), cv2.LINE_AA)
    image = labels(image, locator, scale, f"{map_name} - where the rings end up",
                   [f"{len(matches)} games with rings", "the smallest rings seen; a bigger ring is hidden once",
                    f"{COVERED_BY}+ games had a smaller ring inside it", "number = games that ended there"])
    from PIL import Image, ImageDraw
    pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    for z in zones:
        draw.text((z["x"] * scale, z["y"] * scale), str(z["games"]), font=Match_Map.font(30 if z["games"] > 1 else 22),
                  fill=(255, 200, 60), anchor="mm", stroke_width=4, stroke_fill=(0, 0, 0))
    image = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    out = locator.folder / "rings.png"
    cv2.imwrite(str(out), image)
    (locator.folder / "end_zones.json").write_text(json.dumps(zones, indent=2))
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
