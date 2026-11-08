"""
How you moved in a match, from its trails.json (the read's map positions): glide, distance on foot, how spread
out the squad played, and how far your nearest teammate was when you went down for good.

Only your own trail and your teammates' (never the players spectated after your squad was out); everything after
landing, on foot only, like the heatmap (Heatmap.on_foot: no gaps, nothing faster than Match_Map.FLY_SPEED).
Metres from the map's scale (Map_Locate.PX_PER_M_FIXED, else the px_per_m it learned in Maps/<Map>/info.json):
a map with neither gets nothing.

Worked out once per match and kept in Captures/.notes/movement.json (by trails.json's change time), so the
Control Panel's load stays quick.

Usage:
    .venv/bin/python Movement.py Captures/<match>      # one match's numbers
    .venv/bin/python Movement.py                       # every captured match, a line each
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import Map_Locate
import Match_Map

HERE = Path(__file__).parent
CAPTURES = HERE / "Captures"
MAPS = HERE / "Maps"
CACHE = CAPTURES / ".notes" / "movement.json"
VERSION = 3                 # bump when a number changes meaning: every match is worked out again

TRAIL_GAP = Match_Map.TRAIL_GAP     # s; unseen longer than this = lost, no move counted across it
FLY_SPEED = Match_Map.FLY_SPEED     # share of the map width a second; faster isn't on foot
PAIR_WITHIN = 1             # s; a teammate's sighting this close in time to yours counts as "at the same time"
PAIRED_MIN = 30             # sightings with a teammate at the same time needed for spread / together
TOGETHER_M = 50             # m; "near a teammate" - your nearest teammate this close
ALONE_M = 150               # m; "on your own" - every teammate further than this
LAST_FIGHT = 30             # s before your trail ended at your death: "your last fight" for death_gap
SHIP_MOST = 90              # s; longer from the dropship screen to the jump isn't one flight (a redeploy's jump):
                            # the ship crosses a map in ~60 s


def map_scale(name, cache={}):
    """A map's px per game metre (Map_Locate's fixed one, else what it learned in info.json), or None."""
    if name not in cache:
        info = MAPS / (name or "") / "info.json"
        try:
            learned = json.loads(info.read_text()).get("px_per_m") if name and info.exists() else None
        except (OSError, ValueError):
            learned = None
        cache[name] = Map_Locate.PX_PER_M_FIXED.get(name) or learned
    return cache[name]


def seconds_of(stamp):
    """A trails.json time ("2026-11-06T01:15:14") as whole seconds, for lining sightings up."""
    return int(datetime.fromisoformat(stamp).timestamp())


def median(values):
    values = sorted(values)
    n = len(values)
    return None if not n else values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def match_movement(data, scale=None):
    """The numbers for one trails.json (a dict), metres and seconds; None where it can't be told:
        glide       jump off the dropship -> landing (None in Ranked - Dropzone: no flight)
        ship_m      dropship distance: how far the dropship flew over the map before you jumped (where its flight
                    line comes onto the map picture -> your jump, on the line)
        walked      on foot after landing
        moving      seconds of those on-foot moves (time you were seen, walking or standing)
        pace        walked a minute of that, m
        spread      the middle (median) distance to your nearest teammate, when you and one were seen at once
        together    share of those moments your nearest teammate was within TOGETHER_M
        alone       share of them every teammate was further than ALONE_M
        death_gap   your nearest teammate in your last fight: the median over the LAST_FIGHT s before your trail
                    ended at your death, of the moments one was seen (a squad wipe ends every trail within
                    seconds, so the moment itself says little); None if none was
        last_up     True when no teammate was seen at all in those LAST_FIGHT s (you were the last of the squad
                    up), False with one, None: no death in trails.json. trails.json's death is where you were at
                    the end on a win too: the Control Panel leaves both out for wins
        paired      how many moments spread / together are from
        players     every squad member's own (yours too): name (as the squad panel read it), is_me, walked, pace,
                    spread / together (to their nearest squadmate), and for teammates gap_me (the median distance
                    to you) and near_me (share of the moments you were both seen that they were within
                    TOGETHER_M of you)"""
    out = dict(glide=None, ship_m=None, walked=None, moving=None, pace=None, spread=None, together=None, alone=None,
               death_gap=None, last_up=None, paired=0, players=[])
    scale = scale or map_scale(data.get("map"))
    if not scale:
        return out
    width = (data.get("reference_size") or [1030])[0]
    dropship = data.get("dropship") if isinstance(data.get("dropship"), dict) else {}
    jump, land = dropship.get("jump"), data.get("landing")
    if jump and land:
        out["glide"] = math.hypot(jump["x"] - land["x"], jump["y"] - land["y"]) / scale
    start = dropship.get("start")
    if jump and start:
        out["ship_m"] = math.hypot(jump["x"] - start["x"], jump["y"] - start["y"]) / scale
    landed = seconds_of(land["time"]) if land and land.get("time") else None

    tracks = []                     # (player, [(second, point)], {second: point}), you first
    for player in sorted(data.get("players") or [], key=lambda p: not p.get("is_me")):
        points = [(seconds_of(p["time"]), p) for p in player.get("points", [])]
        if points:
            tracks.append((player, points, dict(points)))
    me = next((t for t in tracks if t[0].get("is_me")), None)
    if not me:
        return out
    if landed is None:
        landed = me[1][0][0]

    def on_foot(points):
        """(metres, seconds) moved on foot after landing."""
        walked = moving = 0.0
        for (ta, a), (tb, b) in zip(points, points[1:]):
            dt = tb - ta
            if b.get("gap") or dt > TRAIL_GAP or tb <= landed:
                continue
            step = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
            if step > FLY_SPEED * width * max(dt, 1):
                continue                                # a jump tower, balloon, zipline, respawn drop
            walked += step
            moving += dt
        return walked / scale, moving

    def nearest(t, x, y, among):
        """The nearest of `among` ({second: point} each) seen within PAIR_WITHIN s of t, in metres (None: nobody)."""
        best = None
        for seen in among:
            spot = next((seen[t + d] for d in sorted(range(-PAIR_WITHIN, PAIR_WITHIN + 1), key=abs) if t + d in seen),
                        None)
            if spot:
                gap = math.hypot(spot["x"] - x, spot["y"] - y) / scale
                best = gap if best is None else min(best, gap)
        return best

    for player, points, _ in tracks:
        others = [seen for q, _, seen in tracks if q is not player]
        walked, moving = on_foot(points)
        gaps = [g for t, p in points if t > landed for g in [nearest(t, p["x"], p["y"], others)] if g is not None]
        numbers = dict(name=player.get("name"), is_me=bool(player.get("is_me")), walked=walked, moving=moving,
                       pace=walked / moving * 60 if moving >= 30 else None, spread=None, together=None, alone=None,
                       paired=len(gaps))
        if others and len(gaps) >= PAIRED_MIN:
            numbers.update(spread=median(gaps), together=sum(g <= TOGETHER_M for g in gaps) / len(gaps),
                           alone=sum(g > ALONE_M for g in gaps) / len(gaps))
        if player is not me[0]:
            to_me = [g for t, p in points if t > landed for g in [nearest(t, p["x"], p["y"], [me[2]])] if g is not None]
            numbers.update(gap_me=median(to_me) if len(to_me) >= PAIRED_MIN else None,
                           near_me=sum(g <= TOGETHER_M for g in to_me) / len(to_me) if len(to_me) >= PAIRED_MIN else None)
        out["players"].append(numbers)
    mine = out["players"][0]
    out.update({k: mine[k] for k in ("walked", "moving", "pace", "spread", "together", "alone", "paired")})

    mates = [seen for q, _, seen in tracks if q is not me[0]]
    death = data.get("death")
    if death and death.get("time") and mates:
        died = seconds_of(death["time"])
        last = [g for t, p in me[1] if died - LAST_FIGHT <= t <= died
                for g in [nearest(t, p["x"], p["y"], mates)] if g is not None]
        out["last_up"] = not last
        out["death_gap"] = median(last)
    return out


def ship_time(data, events):
    """Seconds from the dropship screen's first frame (match.json) to your jump (trails.json), or None: the
    screen not seen (its IN DROPSHIP banner isn't always up - a quick jump is often missed) or no jump time."""
    dropship = data.get("dropship") if isinstance(data.get("dropship"), dict) else {}
    jumped = (dropship.get("jump") or {}).get("time")
    boarded = next((e.get("time") for e in events if e.get("screen") == "dropship" and e.get("event") == "enter"),
                   None)
    if not jumped or not boarded:
        return None
    seconds = seconds_of(jumped) - seconds_of(boarded)
    return seconds if 0 <= seconds <= SHIP_MOST else None


def load_cache():
    try:
        cache = json.loads(CACHE.read_text())
        return cache if cache.get("version") == VERSION else {"version": VERSION}
    except (OSError, ValueError):
        return {"version": VERSION}


def for_matches(names):
    """{match folder name: match_movement + "ship_s" (ship_time)} for the matches that have a trails.json, from the
    cache where the trails.json hasn't changed since. The cache is written back when anything was worked out."""
    cache, changed, out = load_cache(), False, {}
    for name in names:
        trails = CAPTURES / name / "trails.json"
        try:
            stamp = trails.stat().st_mtime
        except OSError:
            continue
        kept = cache.get(name)
        if kept and kept.get("mtime") == stamp:
            out[name] = kept["movement"]
            continue
        try:
            data = json.loads(trails.read_text())
            out[name] = match_movement(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            events = json.loads((CAPTURES / name / "match.json").read_text()).get("events") or []
            out[name]["ship_s"] = ship_time(data, events)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            out[name]["ship_s"] = None
        cache[name], changed = {"mtime": stamp, "movement": out[name]}, True
    if changed:
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            part = CACHE.with_suffix(".part")
            part.write_text(json.dumps(cache))
            part.replace(CACHE)
        except OSError:
            pass
    return out


def describe(m):
    """One match's numbers as a short line (the Latest Match tab and the command line)."""
    bits = []
    if m.get("walked") is not None:
        bits.append(f"on foot {m['walked'] / 1000:.1f} km" if m["walked"] >= 1000 else f"on foot {m['walked']:.0f} m")
    if m.get("spread") is not None:
        bits.append(f"squad {m['spread']:.0f} m apart ({m['together']:.0%} of the time within {TOGETHER_M} m)")
    if m.get("last_up"):
        bits.append("no teammate near in your last fight")
    elif m.get("death_gap") is not None:
        bits.append(f"nearest teammate {m['death_gap']:.0f} m away in your last fight")
    if m.get("glide") is not None:
        bits.append(f"glided {m['glide']:.0f} m")
    return " · ".join(bits)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
        m = match_movement(json.loads((folder / "trails.json").read_text()))
        print(json.dumps(m, indent=2))
        print(describe(m))
    else:
        names = sorted(p.parent.name for p in CAPTURES.glob("*/trails.json"))
        for name, m in for_matches(names).items():
            print(name, "·", describe(m) or "-")
