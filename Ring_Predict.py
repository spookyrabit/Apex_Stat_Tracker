"""
Where this match's endgame is likely to be, from the rings seen so far and how rings moved in your past games.

How rings move (measured on your games, Captures/*/trails.json):
    * every next ring touches the edge of the ring before: its centre moves 90-100% of the room it has
      (ring radius - next radius), so only the direction is open
    * the direction carries on from the last move (54% of turns are under 45 deg; random would be 25%), and round 1
      already leans away from the map's middle (only 8% of round 1 -> 2 moves head back towards the middle)
    * ring centres stay over the map: the land your squads have walked on (all games' trails, grown a little)
So the rounds still to come are played out a few thousand times (momentum with von Mises spread KAPPA, land
weighting) and the last ring's centre is shaded where it most often ended up.

Back-tested on your games (each game left out of the land it's tested with): given round 1 the brightest half
of the shading held the deepest ring seen 47% of the time (covering ~3% of the map), given rounds 1-2 55%
(~1%). The single most likely spot is 17% closer than the current ring's centre after round 1, and no better
after round 2 - the shading is the useful part.

Usage:
    .venv/bin/python Ring_Predict.py <rings.json | match folder> <out.jpg>
(rings.json: {"map", "rings"} as Match_Map.LiveMap writes Captures/live_rings.json; a match folder: its trails.json)
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

import Map_Locate
import Match_Map

HERE = Path(__file__).parent
CAPTURES = HERE / "Captures"
RUNS = 4000                    # play-outs of the rounds still to come
KAPPA = 2.5                    # von Mises spread of each turn (2.5: median turn ~40 deg, as measured)
MOVE = (0.95, 0.06)            # share of the room each ring moves: mean, spread (clipped to 0.5-1)
OFF_LAND = 0.05                # weight left for a ring centre off the walked-on land (not zero: edge POIs)
LIKELY = 0.5                   # the shading's bright part holds this share of the chance

def walked_land(locator):
    """Where your squads have ever been on this map (every trails.json), grown to fill the gaps: 0-1."""
    h, w = locator.reference.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for path in CAPTURES.glob("*/trails.json"):
        data = json.loads(path.read_text())
        if data.get("map") != locator.map_name:
            continue
        for player in data.get("players", []):
            for p in player["points"]:
                cv2.circle(mask, (int(p["x"]), int(p["y"])), 3, 255, -1)
    if not mask.any():
        return np.ones((h, w), np.float32)
    k = int(0.08 * w) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k // 3 | 1, k // 3 | 1)))
    return cv2.GaussianBlur((mask > 0).astype(np.float32), (0, 0), 0.02 * w)

def known_rounds(locator, rings):
    """{round: ring} of the rings seen so far (a next ring counts: it's where that round's ring will be)."""
    out = {}
    for ring in Match_Map.distinct_rings(rings):
        rnd, _ = locator.ring_round(ring["r"])
        if rnd:
            out.setdefault(rnd, ring)
    return out

def play_out(locator, known, land, rng):
    """Centres of every round still to come, RUNS times: ({round: (RUNS, 2) centres}, weights)."""
    h, w = locator.reference.shape[:2]
    sizes = sorted(locator.ring_sizes, reverse=True)
    k = max(known)
    centre = np.array([known[k]["x"], known[k]["y"]], float)
    before = known.get(k - 1)
    # The way the rings have been moving: from the ring before, or (round 1) from the map's middle
    origin = np.array([before["x"], before["y"]]) if before else np.array([w / 2, h / 2])
    heading = np.arctan2(*(centre - origin)[::-1])
    pts, dirs, weight = np.tile(centre, (RUNS, 1)), np.full(RUNS, heading), np.ones(RUNS)
    out = {}
    for j in range(k + 1, len(sizes) + 1):
        room = sizes[j - 2] - sizes[j - 1]
        dirs = dirs + rng.vonmises(0, KAPPA, RUNS)
        moved = np.clip(rng.normal(*MOVE, RUNS), 0.5, 1.0) * room
        pts = pts + moved[:, None] * np.c_[np.cos(dirs), np.sin(dirs)]
        xi, yi = np.clip(pts[:, 0].astype(int), 0, w - 1), np.clip(pts[:, 1].astype(int), 0, h - 1)
        on_map = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
        weight = weight * (np.where(on_map, land[yi, xi], 0) * (1 - OFF_LAND) + OFF_LAND)
        out[j] = pts.copy()
    return out, weight / weight.sum()

def most_likely(pts, weight, r):
    """The play-out centre with the most (weighted) others within a ring's radius of it."""
    some = slice(None, None, 8)
    near = np.hypot(pts[:, None, 0] - pts[None, some, 0], pts[:, None, 1] - pts[None, some, 1]) < r
    return pts[np.argmax((near @ weight[some]) * weight)]

def density(pts, weight, shape, blur):
    """Weighted play-out centres as a smooth 0-1 map, and the level above which LIKELY of the chance lies."""
    h, w = shape
    grid = np.zeros((h, w), np.float32)
    ok = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
    np.add.at(grid, (pts[ok, 1].astype(int), pts[ok, 0].astype(int)), weight[ok])
    grid = cv2.GaussianBlur(grid, (0, 0), blur)
    order = np.sort(grid.ravel())[::-1]
    cut = order[min(len(order) - 1, np.searchsorted(np.cumsum(order) / max(order.sum(), 1e-12), LIKELY))]
    top = grid.max() or 1
    return grid / top, cut / top

def poi_chances(locator, pts, weight):
    """[(POI, chance)] of the nearest POI to the last ring's centre, most likely first."""
    if not locator.pois:
        return []
    xy = np.array([[p["x"], p["y"]] for p in locator.pois])
    nearest = np.argmin(((pts[:, None, :] - xy[None]) ** 2).sum(axis=2), axis=1)
    chance = np.bincount(nearest, weights=weight, minlength=len(xy))
    return [(locator.pois[i]["name"], float(chance[i])) for i in np.argsort(chance)[::-1] if chance[i] >= 0.03]

def predict(map_name, rings, seed=0):
    """Everything the picture shows: known rounds, the final round's play-outs, best guesses, POI chances."""
    locator = Map_Locate.MapLocator(map_name)
    known = known_rounds(locator, rings)
    sizes = sorted(locator.ring_sizes, reverse=True)
    out = {"locator": locator, "known": known, "sizes": sizes}
    if not known or max(known) >= len(sizes):
        return out
    centres, weight = play_out(locator, known, walked_land(locator), np.random.default_rng(seed))
    last, nxt = len(sizes), max(known) + 1
    out.update(weight=weight, final=centres[last], final_r=sizes[last - 1],
               best=most_likely(centres[last], weight, sizes[last - 1]),
               next_best=most_likely(centres[nxt], weight, sizes[nxt - 1]), next_round=nxt, next_r=sizes[nxt - 1],
               pois=poi_chances(locator, centres[last], weight))
    return out

def draw(result, out_file, title=""):
    locator = result["locator"]
    h, w = locator.reference.shape[:2]
    scale = Match_Map.OUTPUT_SIZE / max(h, w)
    size = (int(w * scale), int(h * scale))
    import Map_Icons
    base = cv2.imread(str(Map_Icons.drawing_reference(locator.folder)))      # without icons / printed POI names
    base = base if base is not None and base.shape == locator.reference.shape else locator.reference
    image = (cv2.resize(base, size, interpolation=cv2.INTER_AREA).astype(np.float32) * 0.5).astype(np.uint8)
    P = lambda x, y: (int(x * scale), int(y * scale))
    lines = []
    if "final" in result:
        heat, cut = density(result["final"], result["weight"], (h, w), max(3.0, result["final_r"] * 0.6))
        heat = cv2.resize(heat, size, interpolation=cv2.INTER_LINEAR)
        colour = cv2.applyColorMap(np.uint8(np.clip(heat, 0, 1) * 255), cv2.COLORMAP_TURBO).astype(np.float32)
        alpha = (np.clip(heat * 2.5, 0, 0.75) * (heat > cut * 0.35))[..., None]
        image = (image * (1 - alpha) + colour * alpha).astype(np.uint8)
        likely = (heat >= cut).astype(np.uint8)
        contours, _ = cv2.findContours(likely, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(image, contours, -1, (255, 255, 255), 2, cv2.LINE_AA)
        bx, by = result["best"]
        cv2.circle(image, P(bx, by), int(result["final_r"] * scale), (60, 220, 255), 3, cv2.LINE_AA)
        nx, ny = result["next_best"]
        cv2.circle(image, P(nx, ny), int(result["next_r"] * scale), (120, 200, 255), 2, cv2.LINE_AA)
    for rnd, ring in sorted(result["known"].items()):
        cv2.circle(image, P(ring["x"], ring["y"]), int(ring["r"] * scale), (245, 245, 245), 2, cv2.LINE_AA)
    known = sorted(result["known"])
    if not known:
        lines.append("No ring seen yet: open the map once the first ring shows")
    elif "final" not in result:
        lines.append("The last ring is already known")
    else:
        lines.append(f"Seen: round {', '.join(map(str, known))}  ·  guessing rounds {known[-1] + 1}-{len(result['sizes'])}")
        if result["pois"]:
            lines.append("Endgame most likely near: " + ", ".join(f"{name} {chance:.0%}" for name, chance in result["pois"][:4]))
        lines.append("Shading: where the last ring's centre ends up; inside the white line = half the chance")
        lines.append("Yellow circle: most likely last ring  ·  orange: most likely next ring (round "
                     f"{result['next_round']})")
        lines.append("Back-tests on your games: the white line held the real ring about half the time")
    from Heatmap import labels
    image = labels(image, locator, scale, title or f"{locator.map_name} - endgame guess", lines)
    cv2.imwrite(str(out_file), image)
    return out_file

def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, out = Path(sys.argv[1]), sys.argv[2]
    data = json.loads((source / "trails.json" if source.is_dir() else source).read_text())
    result = predict(data["map"], data.get("rings", []))
    draw(result, out)
    for name, chance in result.get("pois", [])[:5]:
        print(f"{name}: {chance:.0%}")

if __name__ == "__main__":
    main()
