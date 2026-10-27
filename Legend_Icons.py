"""
Which legend each player is, from the portrait next to their name on the squad panel (bottom left, in game).
A fallback for matches where legend select wasn't seen: the portraits are the Legend Locker's, tinted with
the player's colour and cut to a slanted frame, so they're matched in grayscale on the face.

Reference portraits: Screen_References/legend_icons/<Legend>.png, cut from a 1080p screenshot of the Legend
Locker (Main Menu > Legend Locker > Legends). A portrait named anything that isn't a legend in
Extract_Stats.LEGENDS ("Recon 3") is still matched against, so it can't be mistaken for another legend, but
never given as the answer: rename the file to the legend to use it.

Usage:
    .venv/bin/python Legend_Icons.py --from-locker "Screenshot.png"     # cut the reference portraits
    .venv/bin/python Legend_Icons.py Captures/<match>                    # who the squad panel says played what
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2

# ---------------------------
# CONFIGURATION
# ---------------------------

ICONS_FOLDER = Path(__file__).parent / "Screen_References" / "legend_icons"
# The Legend Locker at 1080p: top of each row of portraits -> left edge of each portrait, in the game's order
# (Assault, Skirmisher on top; Recon, Support, Controller below), and who they are in that order
LOCKER_ROWS = {711: [388, 471, 554, 637, 720, 887, 970, 1053, 1136, 1219, 1302, 1386],
               851: [180, 262, 345, 429, 512, 595, 762, 845, 927, 1011, 1094, 1177, 1344, 1426, 1509, 1593]}
LOCKER_LEGENDS = ["Bangalore", "Revenant", "Fuse", "Mad Maggie", "Ballistic",
                  "Pathfinder", "Wraith", "Octane", "Horizon", "Ash", "Alter", "Axle",
                  "Bloodhound", "Crypto", "Valkyrie", "Seer", "Vantage", "Sparrow",
                  "Gibraltar", "Lifeline", "Mirage", "Loba", "New Castle", "Conduit",
                  "Caustic", "Wattson", "Rampart", "Catalyst"]
PORTRAIT = (67, 80)            # px (w, h) kept of each portrait, inside its frame
FACE = (10, 8, 57, 62)         # the head inside a portrait (x0, y0, x1, y1): what's matched
# Where the portrait sits on the squad panel, from the top-left of the player's name (1080p): teammates have a
# small one, you a big one, and the face is shown smaller than in the locker
SEARCH_MATE = (-75, -15, -5, 50)
SEARCH_ME = (-95, -20, -20, 70)
SCALES_MATE = [0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8]
SCALES_ME = [0.55, 0.65, 0.75, 0.85, 0.95, 1.05]
MIN_SCORE = 0.7                # match strength a portrait needs to count (the right one scores ~0.9, others ~0.6)
MIN_MARGIN = 0.15              # ...and how far ahead of the next legend it must be
TRIES = 12                     # squad panel shots looked at per match
MIN_VOTES = 2                  # shots that must agree on a player's legend

# ---------------------------
# MATCHING
# ---------------------------

_icons = None


def prepare(image):
    """Grayscale with local contrast evened out: the player's colour tint mostly drops out."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(2.0, (4, 4)).apply(gray)


def icons():
    """{name: prepared face} for every reference portrait."""
    global _icons
    if _icons is None:
        x0, y0, x1, y1 = FACE
        _icons = {path.stem: prepare(cv2.imread(str(path)))[y0:y1, x0:x1]
                  for path in sorted(ICONS_FOLDER.glob("*.png"))}
    return _icons


def identify(region, scales):
    """[(name, score)] for the portrait in `region`, best first."""
    target = prepare(region)
    scores = {}
    for name, face in icons().items():
        best = -1.0
        for scale in scales:
            template = cv2.resize(face, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if template.shape[0] <= target.shape[0] and template.shape[1] <= target.shape[1]:
                best = max(best, float(cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED).max()))
        scores[name] = best
    return sorted(scores.items(), key=lambda kv: -kv[1])


def panel_legends(panel, s=1.0):
    """{name on the panel: legend} for each player whose portrait is a clear match (s = panel scale from 1080p)."""
    from Extract_Stats import LEGENDS, is_me, read_text
    import Match_Map
    out = {}
    for x, y, text in read_text(panel):
        name = Match_Map.clean_panel_name(text)
        if not name:
            continue
        mine = is_me(name)
        x0, y0, x1, y1 = SEARCH_ME if mine else SEARCH_MATE
        region = panel[max(0, y + int(y0 * s)):max(0, y + int(y1 * s)), max(0, x + int(x0 * s)):max(0, x + int(x1 * s))]
        if region.shape[0] < 20 * s or region.shape[1] < 20 * s:
            continue
        ranked = identify(region, [k * s for k in (SCALES_ME if mine else SCALES_MATE)])
        (best, score), (_, second) = ranked[0], ranked[1]
        if best in LEGENDS and score >= MIN_SCORE and score - second >= MIN_MARGIN:
            out[name] = best
    return out


def squad_legends(folder, events):
    """{name: legend} for the squad, voted over squad panel shots taken while alive in game."""
    import Match_Map
    from Extract_Stats import name_similarity
    shots = [e for e in events if e.get("file") and e["screen"] == "in_game" and e["event"] in ("squad", "full")]
    shots = shots[:: max(1, len(shots) // TRIES)][:TRIES]
    votes = defaultdict(Counter)                  # first spelling of a name -> legend votes
    for e in shots:
        image = cv2.imread(str(Path(folder) / e["file"]))
        if image is None:
            continue
        panel, s = Match_Map.squad_panel_of(image) if e["event"] == "full" else \
            (image, image.shape[1] / (Match_Map.SQUAD_PANEL[2] - Match_Map.SQUAD_PANEL[0]))
        for name, legend in panel_legends(panel, s).items():
            known = next((n for n in votes if name_similarity(n, name) >= 0.7), name)
            votes[known][legend] += 1
    out = {}
    for name, counts in votes.items():
        (legend, n), total = counts.most_common(1)[0], sum(counts.values())
        if n >= MIN_VOTES and n >= 2 * total / 3:
            out[name] = legend
    return out

# ---------------------------
# MAIN SCRIPT
# ---------------------------

def cut_from_locker(path):
    image = cv2.imread(str(path))
    if image is None:
        sys.exit(f"Can't read {path}")
    image = cv2.resize(image, (1920, 1080), interpolation=cv2.INTER_AREA) if image.shape[1] != 1920 else image
    ICONS_FOLDER.mkdir(parents=True, exist_ok=True)
    corners = [(x, y) for y, xs in LOCKER_ROWS.items() for x in xs]
    w, h = PORTRAIT
    for (x, y), name in zip(corners, LOCKER_LEGENDS):
        cv2.imwrite(str(ICONS_FOLDER / f"{name}.png"), image[y + 2:y + 2 + h, x + 2:x + 2 + w])
    print(f"Cut {len(corners)} portraits into {ICONS_FOLDER}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("match", nargs="?", help="match folder to read the squad panel of")
    parser.add_argument("--from-locker", metavar="SCREENSHOT", help="cut the reference portraits from a Legend Locker screenshot")
    args = parser.parse_args()
    if args.from_locker:
        cut_from_locker(args.from_locker)
    elif args.match:
        events = json.loads((Path(args.match) / "match.json").read_text())["events"]
        print(squad_legends(args.match, events))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
