"""
All your games on one map: where players go, and where the rings went.

For every map, from the trails.json of every match played on it, transparent layers the Control Panel's Maps tab
stacks over the map (no map, POI names or titles on them):
    Maps/<Map>/layer_heatmap.png   how many player-trips passed through each spot (you, teammates and the players you
                                   spectated after your squad was out, after landing, on foot: skydives, jump towers and balloons left out). A player standing still
                                   counts once, so busy routes, rotations and choke points light up, not just camping
                                   spots. Landings are green dots.
    Maps/<Map>/layer_rings.png     where endgames end up: the smallest rings seen (a bigger ring is hidden once 3+
                                   games had a smaller ring inside it; only the last 3 ring sizes), the same spot in
                                   different games merged into one circle with a count. Also end_zones.json.
    Maps/<Map>/layer_care_package.png, layer_stronghold.png, layer_evo_harvester.png
                                   where Care Packages / Strongholds / Evo Harvesters turn up (Map_Icons.DRAWN, each match's trails.json
                                   items): heat in the icon's colour, a dot on each one.

Usage:
    python Heatmap.py               # every map
    python Heatmap.py "Broken Moon"  # one map
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import Map_Locate
import Map_View
import Match_Map

HERE = Path(__file__).parent
CAPTURES = HERE / "Captures"
TRAIL_WIDTH = 0.004            # share of the map width a trail covers
BLUR = 0.004                   # share of the map width the heat is smoothed over
TRAIL_GAP = Match_Map.TRAIL_GAP
FLY_SPEED = Match_Map.FLY_SPEED     # faster than this is flying, not walking (share of the map width a second)

def load_matches(map_name):
    from Extract_Stats import RESPAWN_MODES
    out = []
    for path in sorted(CAPTURES.glob("*/trails.json")):
        data = json.loads(path.read_text())
        # (respawn modes' trails jump all over and their rings start early: not this map's usual games)
        if data.get("map") == map_name and data.get("mode") not in RESPAWN_MODES:
            out.append(data)
    return out

def paths_in(data):
    """Every path in one trails.json: your squad's players, then the players you spectated after it was out
    (other squads' paths: only for where people go - heatmaps, the endgame guess's land and route)."""
    return data.get("players", []) + data.get("spectated", [])

def on_foot(points, landed_at, map_width):
    """[(a, b)] the moves one player made walking (after landing, no gap, slower than FLY_SPEED)."""
    times = [datetime.fromisoformat(p["time"]) for p in points]
    out = []
    for (a, ta), (b, tb) in zip(zip(points, times), zip(points[1:], times[1:])):
        dt = (tb - ta).total_seconds()
        if b.get("gap") or dt > TRAIL_GAP or (landed_at and tb <= landed_at):
            continue
        if np.hypot(b["x"] - a["x"], b["y"] - a["y"]) > FLY_SPEED * map_width * max(dt, 1):
            continue
        out.append((a, b))
    return out

def trail_mask(points, landed_at, shape, width):
    """Where one player walked in one match (after landing, not flying; no lines across gaps), as a 0/1 mask."""
    mask = np.zeros(shape, np.uint8)
    for a, b in on_foot(points, landed_at, shape[1]):
        cv2.line(mask, (int(a["x"]), int(a["y"])), (int(b["x"]), int(b["y"])), 1, width, cv2.LINE_AA)
    return mask

def colour_heat(heat):
    """Heat (0-1) -> BGR colour and alpha: blue (rare) through yellow to red (busiest)."""
    lut = cv2.applyColorMap(np.uint8(np.clip(heat, 0, 1) * 255), cv2.COLORMAP_TURBO)
    alpha = np.clip(heat * 3, 0, 0.85)[..., None]
    return lut, alpha

class Layer:
    """A transparent picture the size of the map pictures, for the Maps tab to stack over the map (no map, POI names
    or titles on it: the tab adds those). Built a piece at a time, each covering what's under it by its alpha. What has
    edges (dots, outlines, counts, keys) goes in `marks` instead (Map_View: shapes in map-picture px, drawn sharp at
    any zoom); save() writes both, and an SVG of the two."""

    def __init__(self, size, locator=None):
        w, h = size
        self.colour = np.zeros((h, w, 3), np.float32)      # premultiplied by alpha
        self.alpha = np.zeros((h, w), np.float32)
        self.marks = []
        self.ref_size = tuple(locator.reference.shape[1::-1]) if locator else None
        self.title = locator.map_name if locator else ""

    def mark(self, **mark):
        """A Map_View mark: type="dot" / "circle" / "poly" / "text" / "item" / "key", colours as BGR or "#rrggbb"."""
        for key in ("fill", "edge", "colour"):
            if isinstance(mark.get(key), (tuple, list)):
                mark[key] = Map_View.hex_colour(mark[key])
        self.marks.append(mark)

    def over(self, bgr, alpha):
        """`alpha`: (h, w), 0-1; `bgr`: one colour, or (h, w, 3)."""
        a = np.clip(np.asarray(alpha, np.float32), 0, 1)
        self.colour = np.asarray(bgr, np.float32) * a[..., None] + self.colour * (1 - a[..., None])
        self.alpha = a + self.alpha * (1 - a)

    def draw(self, paint, bgr, opacity=1.0):
        """`paint(mask)`: cv2 drawing in 255 on a blank mask (anti-aliased edges come out see-through)."""
        mask = np.zeros(self.alpha.shape, np.uint8)
        paint(mask)
        self.over(bgr, mask / 255.0 * opacity)

    def image(self):
        """The layer as an RGBA PIL image."""
        from PIL import Image
        a = np.maximum(self.alpha, 1e-6)[..., None]
        rgb = np.clip(self.colour / a, 0, 255)[..., ::-1]
        return Image.fromarray(np.dstack([rgb, self.alpha[..., None] * 255]).astype(np.uint8), "RGBA")

    def save(self, path, image=None):
        """The picture as `path`; with a locator, the marks as <path>.json and both as <path>.svg."""
        path = Path(path)
        (image or self.image()).save(path)
        if self.ref_size:
            tmp = path.with_name(f".{path.stem}.json.tmp")
            tmp.write_text(json.dumps({"size": list(self.ref_size), "marks": self.marks}))
            tmp.replace(path.with_suffix(".json"))
            Map_View.write_svg(path.with_suffix(".svg"), self.ref_size, [(path, 1.0)], self.marks, self.title)
        return path

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

def heatmap(map_name):
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
        for player in paths_in(data):
            mask = trail_mask(player["points"], landed_at, (h, w), width)
            if mask.any():
                heat += mask
                trips += 1
    if trips == 0:
        return None
    heat = cv2.GaussianBlur(heat, (0, 0), max(1.0, BLUR * w))
    top = np.percentile(heat[heat > 0], 99) if (heat > 0).any() else 1
    norm = heat / max(top, 1e-6)

    scale, size = layer_size(locator)
    layer = Layer(size, locator)
    colour, alpha = colour_heat(cv2.resize(norm, size, interpolation=cv2.INTER_LINEAR))
    layer.over(colour, alpha[..., 0])
    for land in landings:
        layer.mark(type="dot", x=land["x"], y=land["y"], px=5, fill=(80, 255, 80), edge=(0, 0, 0))
    return layer.save(locator.folder / "layer_heatmap.png")

def layer_size(locator):
    """(scale from reference px, (w, h)) of the map layers: the match pictures' size."""
    h, w = locator.reference.shape[:2]
    scale = Match_Map.OUTPUT_SIZE / max(h, w)
    return scale, (int(w * scale), int(h * scale))

COVERED_BY = 3                 # a ring is hidden once this many games had a smaller ring inside it
SMALLEST_STAGES = 3            # only the last few ring stages (sizes) are shown
ROUND_COLOURS = {2: (255, 160, 60), 3: (60, 200, 255), 4: (80, 80, 255), 5: (230, 90, 230),   # BGR: blue, yellow, red, pink,
                 6: (120, 230, 120)}                                                             # green (Storm Point's held round 6)
RING_TRACE = 6                 # ring group outlines are traced at this many px per map px
RING_HALO = 3                  # px the dark edge under each ring's outline sticks out (both sides together)
MERGE_AT = 0.7                 # a ring joins a host ring (same stage) when they overlap this much (area)

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

def end_zones(matches, map_width=None, sizes=(), pois=(), set_size=None):
    """The likely end zones on a map: the smallest rings seen, merged where they're the same spot.
    `sizes`: the map's known ring sizes (rings caught mid-close in older games are left out). `set_size(r)`: the
    round's set size for a ring this big, or None (MapLocator.ring_size): each ring is drawn at its round's size,
    and one that's no round's (caught while closing) is left out.
    Returns [{"x", "y", "r", "games"}] smallest first."""
    rings = []
    for i, data in enumerate(matches):
        # One game sees the same ring several times (map, minimap): one copy each
        own = [r for r in data.get("rings", []) if r["kind"] == "ring" and (not map_width or r["r"] >= 0.015 * map_width)
               and Match_Map.ring_stage_check(r, sizes)]
        for g in merge(own, lambda a, b: overlap(a, b) >= 0.8):
            ring = {"game": i, **{k: float(np.median([m[k] for m in g])) for k in ("x", "y", "r")}}
            if set_size:
                ring["r"] = set_size(ring["r"])
                if ring["r"] is None:
                    continue
            rings.append(ring)
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
    # Per stage (ring size): the rings closest to a POI become hosts, others join the host they overlap
    # most (one group per ring, so groups never double count); hosts never merge, so a blob stays about one ring wide
    out = []
    for i, start in enumerate(tiers):
        stage = [r for r in rings if r["r"] >= start - 1e-6 and (i + 1 == len(tiers) or r["r"] < tiers[i + 1] - 1e-6)]
        near = lambda r: min((np.hypot(r["x"] - p["x"], r["y"] - p["y"]) for p in pois), default=0)
        hosts = []
        for r in sorted(stage, key=near):
            if not any(overlap(h["ring"], r) >= MERGE_AT for h in hosts):
                hosts.append({"ring": r, "members": [r]})
        for r in stage:                                    # each ring joins only the host it overlaps most
            if any(r is h["ring"] for h in hosts):
                continue
            best = max(hosts, key=lambda h: overlap(h["ring"], r))
            if overlap(best["ring"], r) >= MERGE_AT:
                best["members"].append(r)
        for h in hosts:
            ms, r = h["members"], h["ring"]
            out.append({"x": r["x"], "y": r["y"], "r": r["r"], "games": len({m["game"] for m in ms}),
                        "members": [{k: round(m[k], 1) for k in ("x", "y", "r")} for m in ms]})
    return sorted(out, key=lambda z: z["r"])

def ring_history(map_name):
    locator = Map_Locate.MapLocator(map_name)
    matches = [d for d in load_matches(map_name) if any(r["kind"] == "ring" for r in d.get("rings", []))]
    if not matches:
        return None
    h, w = locator.reference.shape[:2]
    zones = end_zones(matches, w, locator.ring_sizes, locator.pois, locator.ring_size)
    if not zones:
        return None
    scale, size = layer_size(locator)
    layer = Layer(size, locator)
    for z in zones:
        z["round"] = locator.ring_round(z["r"])[0]
    top = max(z["games"] for z in zones)
    for z in sorted(zones, key=lambda z: -z["r"]):        # big first, so small ones sit on top
        colour = ROUND_COLOURS.get(z["round"], (200, 200, 200))
        # The union of the group's rings as outlines in map px, traced RING_TRACE x finer than the map picture so they
        # stay round zoomed in (Map_View.MAX_ZOOM); only the part of the map round the group
        x0 = int(min(m["x"] - m["r"] for m in z["members"])) - 2
        y0 = int(min(m["y"] - m["r"] for m in z["members"])) - 2
        x1 = int(max(m["x"] + m["r"] for m in z["members"])) + 3
        y1 = int(max(m["y"] + m["r"] for m in z["members"])) + 3
        k = RING_TRACE
        mask = np.zeros(((y1 - y0) * k, (x1 - x0) * k), np.uint8)
        for m in z["members"]:
            cv2.circle(mask, (int((m["x"] - x0) * k), int((m["y"] - y0) * k)), int(m["r"] * k), 255, -1)
        contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
        # every outline on a dark edge a little wider than it (RING_HALO), so it shows over the other layers (the
        # heatmap's blues, the land's greens and yellow line)
        outlines = [[(x0 + float(x) / k, y0 + float(y) / k) for x, y in cv2.approxPolyDP(c, 0.15 * k, True)[:, 0]]
                    for c in contours if len(c) >= 3]
        strength = z["games"] / top
        for points in outlines:
            if z["games"] < 2:                             # one game: just its outline, no fill to blur the rest
                layer.mark(type="poly", points=points, colour=colour, width=2, halo=RING_HALO, fill=0)
            else:                                          # the blob: how much ground the group's rings cover
                layer.mark(type="poly", points=points, colour=colour, width=3 + int(2 * strength), halo=RING_HALO,
                           fill=0.12 + 0.18 * strength)
    layer.mark(type="key", entries=[{"colour": Map_View.hex_colour(ROUND_COLOURS.get(n, (200, 200, 200))), "text": f"Round {n}"}
                                    for n in sorted({z["round"] for z in zones if z["round"]})])
    for z in zones:
        if z["games"] > 1:
            layer.mark(type="text", x=z["x"], y=z["y"], text=str(z["games"]), px=30,
                       colour=ROUND_COLOURS.get(z["round"], (200, 200, 200)), anchor="mm")
    out = layer.save(locator.folder / "layer_rings.png")
    (locator.folder / "end_zones.json").write_text(json.dumps(zones, indent=2))
    return out

ITEM_BLUR = 0.015              # share of the map width one item's heat spreads over
ITEM_HEAT = {"care_package": (255, 255, 255), "stronghold": (110, 45, 255), "evo_harvester": (40, 225, 235)}   # BGR
# Oldest items_version a kind is taken from: Care Packages / Strongholds only from S31's templates on; the Evo Harvester's
# template hasn't changed (S31 games read before 2026-11-05 may hold a white Care Package marker taken for one)
ITEM_SINCE = {"care_package": 2, "stronghold": 2, "evo_harvester": 0}

def item_points(map_name, kind):
    """[(x, y)] of one kind of item (Map_Icons.DRAWN) in every game on this map, and how many games those were.
    Only matches read with templates good for that kind (ITEM_SINCE, trails.json items_version): S30's grey robot
    template found 6-21 "Care Packages" a game in tight clusters."""
    points, games = [], 0
    for data in load_matches(map_name):
        if data.get("items_version", 0) < ITEM_SINCE[kind]:
            continue
        games += 1
        points += [(i["x"], i["y"]) for i in data.get("items", []) if i["kind"] == kind]
    return points, games

def item_heatmap(map_name, kind):
    """Maps/<Map>/layer_<kind>.png: where that kind of item turns up, as heat in its own colour (stronger = more games),
    a dot on each one, and a count top left."""
    import Map_Icons
    points, games = item_points(map_name, kind)
    if not points:
        return None
    locator = Map_Locate.MapLocator(map_name)
    h, w = locator.reference.shape[:2]
    heat = np.zeros((h, w), np.float32)
    for x, y in points:
        if 0 <= int(y) < h and 0 <= int(x) < w:
            heat[int(y), int(x)] += 1
    heat = cv2.GaussianBlur(heat, (0, 0), max(1.0, ITEM_BLUR * w))
    norm = heat / max(heat.max(), 1e-6)
    scale, size = layer_size(locator)
    colour = ITEM_HEAT[kind]
    layer = Layer(size, locator)
    layer.over(colour, np.clip(cv2.resize(norm, size, interpolation=cv2.INTER_LINEAR), 0, 1) * 0.7)
    for x, y in points:
        layer.mark(type="dot", x=x, y=y, px=3, fill=colour, edge=(0, 0, 0))
    layer.mark(type="key", entries=[{"kind": kind, "text": f"{Map_Icons.ITEMS[kind][0]}s: {len(points)} in {games} games"}])
    return layer.save(locator.folder / f"layer_{kind}.png")

def item_heatmaps(map_name):
    import Map_Icons
    return [item_heatmap(map_name, kind) for kind in Map_Icons.DRAWN]

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("map", nargs="?", choices=Map_Locate.MAP_NAMES)
    args = parser.parse_args()
    for name in [args.map] if args.map else Map_Locate.MAP_NAMES:
        made = [heatmap(name), ring_history(name), *item_heatmaps(name)]
        made = [str(m) for m in made if m]
        if made:
            print(f"{name}: " + ", ".join(made))

if __name__ == "__main__":
    main()
