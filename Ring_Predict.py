"""
Where this match's endgame is likely to be, from the rings seen so far and how rings moved in your past games.

How rings move (measured on your games, Captures/*/trails.json):
    * every next ring touches the edge of the ring before: its centre moves 90-100% of the room it has
      (ring radius - next radius), so only the direction is open
    * the direction carries on from the last move (54% of turns are under 45 deg; random would be 25%), and round 1
      already leans away from the map's middle (only 8% of round 1 -> 2 moves head back towards the middle)
    * ring centres stay over the map: the last two rounds' centres over land your squads have walked on (all games'
      trails, grown a little, gaps between paths not filled in: a mountain everyone walks round stays out)
    * turns measured between rounds: 2->3 median 36 deg (54% under 45), 3->4 median 57 - so KAPPA 1.5
The most likely last ring is picked from the play-outs going through the most likely next ring (every ring is
inside the one before; picked from all of them it could sit in another branch, outside the next ring).
So the rounds still to come are played out a few thousand times (momentum with von Mises spread KAPPA, land
weighting) and the last ring's centre is shaded where it most often ended up.

Back-tested (`--backtest`: each game left out of the land it's tested with; 2026-10-31, 102 games with a ring
past round 1, 66 past round 2): the white line (half the chance) held the deepest ring seen 54% of the time given
round 1 (~2.5% of the map), 45% given rounds 1-2 (~0.8%) - near the 50% it should; with KAPPA 2.5 it was 44% / 39%
(too sure). Log score (how much chance the shading puts where the ring really went) +0.25 / +0.34 over KAPPA 2.5,
i.e. ~1.3x / 1.4x. Land on the last two rounds only, gaps not filled (2026-11-01, 106 / 69 games): 57% / 51%, log
score 2.44 / 3.56 (2.43 / 3.54 before) - about the same: real ring centres were on walked ground (median 0 px away)
and so were most play-outs, so it mostly keeps the shading off the odd hole. Tried and dropped: weighting by where other games' rings of each round were (any strength made
it worse: 29-45% held) - past endgames don't say where this one goes beyond how rings move.

Usage:
    .venv/bin/python Ring_Predict.py <rings.json | match folder> <out.jpg>
    .venv/bin/python Ring_Predict.py --backtest
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
KAPPA = 1.5                    # von Mises spread of each turn: best log score in the back-test (1.2-1.8 about
                               # the same; 2.5, the turns measured one at a time, made the shading too sure)
MOVE = (0.95, 0.12)            # share of the room each ring moves: mean, spread (clipped to 0.5-1)
OFF_LAND = 0.01                # weight left for a ring centre off the walked-on land (not zero: edge POIs)
# The last rings form where players can get to: their centres weighed by the land squads have walked, the gaps between
# paths NOT filled in (a mountain your squads go round stays out). Earlier rings, far bigger, can sit over anything.
LAND_ROUNDS = 2                # the last this many rounds
LAND_CLOSE = 0.02              # gaps between paths filled up to this share of the map wide (0.08 fills mountains)
ROUTE_CLOSE = 0.08             # the ring-safe route's land: gaps filled, so it's not cut off by unwalked strips
NEAR_NEXT = 0.25               # the most likely last ring is picked from play-outs whose next ring is within this
                               # share of its radius of the most likely next ring (so yellow sits inside orange)
LIKELY = 0.5                   # the shading's bright part holds this share of the chance
# The ring-safe route (from you to the most likely last ring): cost per px of each kind of ground
ROUTE_CELL = 5                 # px of the map picture per routing step
TRAIL_COST = 1.0               # where squads have walked (busiest corridors cheapest: up to 1.6x for rarely used)
LAND_COST = 75.0                # land nobody has walked on (may be a cliff / lava / no way up)
VOID_COST = 150.0               # off the walked-on land altogether
STORM_COST = 100.0              # extra outside the smallest ring seen (storm now / very soon)
NEXT_RING_COST = 3.0           # extra times the chance the next ring (not seen yet) leaves that spot out

def walked_land(locator, skip=None, close=LAND_CLOSE):
    """Where your squads have ever been on this map (every trails.json but match `skip`), gaps up to `close` of the
    map's width filled and grown a little: 0-1."""
    h, w = locator.reference.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for path in CAPTURES.glob("*/trails.json"):
        data = json.loads(path.read_text())
        if data.get("map") != locator.map_name or path.parent.name == skip:
            continue
        for player in data.get("players", []):
            for p in player["points"]:
                cv2.circle(mask, (int(p["x"]), int(p["y"])), 3, 255, -1)
    if not mask.any():
        return np.ones((h, w), np.float32)
    k = int(close * w) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    g = int(0.08 * w) // 3 | 1
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (g, g)))
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
        if j > len(sizes) - LAND_ROUNDS:
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

def trail_heat(locator):
    """How many squad trips crossed each spot after landing (Heatmap's trails, every game on this map)."""
    import Heatmap
    from datetime import datetime
    h, w = locator.reference.shape[:2]
    heat = np.zeros((h, w), np.float32)
    for data in Heatmap.load_matches(locator.map_name):
        landed = datetime.fromisoformat(data["landing"]["time"]) if data.get("landing") else None
        for player in data["players"]:
            heat += Heatmap.trail_mask(player["points"], landed, (h, w), 3)
    return heat

def ring_safe_route(locator, known, start, goal, next_centres, weight, next_r, land):
    """The cheapest way from `start` to `goal` (x, y on the map picture): along ground squads have walked, out
    of the storm, inside where the next ring will probably be. [(x, y)] or None."""
    import heapq
    h, w = locator.reference.shape[:2]
    c = ROUTE_CELL
    gh, gw = h // c + 1, w // c + 1
    heat = cv2.resize(trail_heat(locator), (gw, gh), interpolation=cv2.INTER_AREA)
    walked = heat > 0.02
    busy = np.clip(heat / max(np.percentile(heat[walked], 90), 1e-6), 0, 1) if walked.any() else heat
    land = cv2.resize(land, (gw, gh), interpolation=cv2.INTER_AREA)
    step = np.where(walked, TRAIL_COST * (1.6 - 0.6 * busy), np.where(land > 0.5, LAND_COST, VOID_COST))
    ys, xs = np.mgrid[0:gh, 0:gw] * c + c / 2
    smallest = min(known.values(), key=lambda r: r["r"])
    step = step + STORM_COST * (np.hypot(xs - smallest["x"], ys - smallest["y"]) > smallest["r"])
    if next_centres is not None:
        # Chance the next ring covers each spot: its play-out centres, each spread over a disc of its radius
        grid = np.zeros((gh, gw), np.float32)
        ok = (next_centres[:, 0] >= 0) & (next_centres[:, 0] < w) & (next_centres[:, 1] >= 0) & (next_centres[:, 1] < h)
        np.add.at(grid, ((next_centres[ok, 1] // c).astype(int), (next_centres[ok, 0] // c).astype(int)), weight[ok])
        k = int(next_r / c)
        disc = np.zeros((2 * k + 1, 2 * k + 1), np.float32)
        cv2.circle(disc, (k, k), k, 1, -1)
        covered = cv2.filter2D(grid, -1, disc, borderType=cv2.BORDER_CONSTANT)
        step = step + NEXT_RING_COST * (1 - np.clip(covered / max(weight.sum(), 1e-9), 0, 1))
    to_cell = lambda p: (min(gh - 1, max(0, int(p[1] // c))), min(gw - 1, max(0, int(p[0] // c))))
    a, b = to_cell(start), to_cell(goal)
    best = np.full((gh, gw), np.inf)
    came = {}
    best[a] = 0
    queue = [(0.0, a)]
    moves = [(dy, dx, np.hypot(dy, dx)) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
    while queue:
        cost, (y, x) = heapq.heappop(queue)
        if (y, x) == b:
            break
        if cost > best[y, x]:
            continue
        for dy, dx, length in moves:
            ny, nx = y + dy, x + dx
            if 0 <= ny < gh and 0 <= nx < gw:
                new = cost + (step[y, x] + step[ny, nx]) / 2 * length * c
                if new < best[ny, nx]:
                    best[ny, nx] = new
                    came[(ny, nx)] = (y, x)
                    heapq.heappush(queue, (new, (ny, nx)))
    if b != a and b not in came:
        return None
    path, cell = [b], b
    while cell != a:
        cell = came[cell]
        path.append(cell)
    return [(x * c + c / 2, y * c + c / 2) for y, x in reversed(path)]

def predict(map_name, rings, me=None, seed=0):
    """Everything the picture shows: known rounds, the final round's play-outs, best guesses, POI chances, and
    (with `me`: {"x", "y"} where you are) the ring-safe route to the most likely last ring."""
    locator = Map_Locate.MapLocator(map_name)
    known = known_rounds(locator, rings)
    sizes = sorted(locator.ring_sizes, reverse=True)
    out = {"locator": locator, "known": known, "sizes": sizes}
    if not known or max(known) >= len(sizes):
        return out
    centres, weight = play_out(locator, known, walked_land(locator), np.random.default_rng(seed))
    last, nxt = len(sizes), max(known) + 1
    next_best = most_likely(centres[nxt], weight, sizes[nxt - 1])
    # Every ring is inside the one before: the most likely last ring of the play-outs going through that next ring
    # (picked from all of them it could sit in another branch, outside the orange circle), kept inside it
    near = np.hypot(*(centres[nxt] - next_best).T) <= NEAR_NEXT * sizes[nxt - 1]
    if near.sum() < 50:
        near = np.argsort(np.hypot(*(centres[nxt] - next_best).T))[:50]
    best = most_likely(centres[last][near], weight[near], sizes[last - 1])
    room = sizes[nxt - 1] - sizes[last - 1]
    off = best - next_best
    if np.hypot(*off) > room:
        best = next_best + off * room / np.hypot(*off)
    out.update(weight=weight, final=centres[last], final_r=sizes[last - 1], best=best, next_best=next_best,
               next_round=nxt, next_r=sizes[nxt - 1], pois=poi_chances(locator, centres[last], weight))
    if me:
        out["me"] = (me["x"], me["y"])
        out["route"] = ring_safe_route(locator, known, out["me"], out["best"], centres.get(nxt), weight,
                                       sizes[nxt - 1], walked_land(locator, close=ROUTE_CLOSE))
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
    if result.get("route"):
        line = np.array([P(x, y) for x, y in result["route"]], np.int32)
        line = cv2.approxPolyDP(line.reshape(-1, 1, 2), 2, False)
        cv2.polylines(image, [line], False, (0, 0, 0), 9, cv2.LINE_AA)
        cv2.polylines(image, [line], False, (90, 235, 90), 5, cv2.LINE_AA)
    if result.get("me"):
        cv2.circle(image, P(*result["me"]), 10, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.circle(image, P(*result["me"]), 7, (90, 235, 90), -1, cv2.LINE_AA)
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
        if result.get("route"):
            length = sum(np.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(result["route"], result["route"][1:]))
            k = locator.info.get("px_per_m")
            lines.append(f"Green: ring-safe route from you ({f'~{length / k:,.0f} m' if k else f'{length:.0f} px'}) - "
                         "on ground squads have walked, out of the storm, inside the likely next ring")
        elif result.get("me"):
            lines.append("No ring-safe route found from where you are")
    from Heatmap import labels
    image = labels(image, locator, scale, title or f"{locator.map_name} - endgame guess", lines)
    cv2.imwrite(str(out_file), image)
    return out_file

def backtest():
    """Each game with rings past round 1 (2): played out from round 1 (rounds 1-2), with its own trails left out of
    the land. Prints how often the white line held the deepest ring seen, the area inside it, and the log score."""
    import Extract_Stats
    games = {}
    for path in sorted(CAPTURES.glob("*/trails.json")):
        data = json.loads(path.read_text())
        if data.get("map") and data.get("mode") not in Extract_Stats.RESPAWN_MODES:
            games.setdefault(data["map"], []).append((path.parent.name, data["rings"]))
    scores = {1: [], 2: []}
    for map_name, matches in games.items():
        locator = Map_Locate.MapLocator(map_name)
        if not locator.ring_sizes:
            continue
        h, w = locator.reference.shape[:2]
        for name, rings in matches:
            known = known_rounds(locator, rings)
            deepest = max(known, default=0)
            for given in (1, 2):
                if deepest <= given or any(r not in known for r in range(1, given + 1)):
                    continue
                centres, weight = play_out(locator, {r: known[r] for r in range(1, given + 1)},
                                           walked_land(locator, skip=name), np.random.default_rng(0))
                pts, truth = centres[deepest], (known[deepest]["x"], known[deepest]["y"])
                grid, cut = density(pts, weight, (h, w), 0.02 * w)
                tx, ty = int(np.clip(truth[0], 0, w - 1)), int(np.clip(truth[1], 0, h - 1))
                # Log score: the play-outs as chances on an 8 px grid (2% spread evenly, so a miss isn't -inf)
                cells = np.histogram2d(pts[:, 1].clip(0, h - 1), pts[:, 0].clip(0, w - 1), bins=(h // 8, w // 8),
                                       range=((0, h), (0, w)), weights=weight)[0].astype(np.float32)
                cells = cv2.GaussianBlur(cells, (0, 0), 2)
                cells = cells / cells.sum() * 0.98 + 0.02 / cells.size
                log = np.log(cells[min(ty // 8, cells.shape[0] - 1), min(tx // 8, cells.shape[1] - 1)] * cells.size)
                scores[given].append((grid[ty, tx] >= cut, (grid >= cut).mean(), log))
    for given, rows in scores.items():
        rows = np.array(rows, float)
        print(f"given round{'s 1-2' if given == 2 else ' 1'}: {len(rows)} games, white line held the deepest ring "
              f"{rows[:, 0].mean():.0%} (area {rows[:, 1].mean():.1%} of the map), log score {rows[:, 2].mean():.2f}")

def main():
    if sys.argv[1:] == ["--backtest"]:
        return backtest()
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, out = Path(sys.argv[1]), sys.argv[2]
    data = json.loads((source / "trails.json" if source.is_dir() else source).read_text())
    me = data.get("me")                         # live: where you are now
    if not me and data.get("players"):          # a read match: where you were last
        mine = next((p for p in data["players"] if p.get("is_me") and p["points"]), None)
        me = mine["points"][-1] if mine else None
    result = predict(data["map"], data.get("rings", []), me)
    draw(result, out)
    for name, chance in result.get("pois", [])[:5]:
        print(f"{name}: {chance:.0%}")

if __name__ == "__main__":
    main()
