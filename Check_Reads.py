"""
Regression check for the stats screen reader: every match keeps its stats screen (Captures/<match>/summary.jpg).
This reads each one again with the current Extract_Stats and lists every one that now reads differently from the
baseline - what the reader got before your change (BASELINE, saved with --save-baseline; without one, stats.json's
"summary", which older code and other screens filled in: 12 of 396 differ from it with nothing wrong, 2026-11-05).

Usage:
    .venv/bin/python Check_Reads.py --save-baseline   # before changing the reader: what it reads now
    .venv/bin/python Check_Reads.py                   # after: every kept summary (~2 s each) against that
    .venv/bin/python Check_Reads.py --sample 40       # the newest 40
Runs at low priority (nice 15), so it's fine while you play. Exit code 1 if anything reads differently.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import cv2

import Extract_Stats

CAPTURES = Path(__file__).parent / "Captures"
BASELINE = CAPTURES / ".notes" / "summary_reads.json"   # {match: summary as read} (--save-baseline)
MATCH_FIELDS = ("placement", "squad_kills")
CARD_FIELDS = ("name", "kills", "assists", "knocks", "damage", "time", "revives", "respawns")


def differences(stored, now):
    """[(what, stored, now)] between two summary reads (cards matched in order, left to right)."""
    out = []
    if now is None:
        return [("summary", "read", "not recognised")]
    for field in MATCH_FIELDS:
        if stored.get(field) != now.get(field):
            out.append((field, stored.get(field), now.get(field)))
    old, new = stored.get("players", []), now.get("players", [])
    if len(old) != len(new):
        out.append(("cards", len(old), len(new)))
    for i, (a, b) in enumerate(zip(old, new), 1):
        for field in CARD_FIELDS:
            if a.get(field) != b.get(field):
                out.append((f"card {i} {field}", a.get(field), b.get(field)))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", type=int, help="only the newest N summaries")
    parser.add_argument("--save-baseline", action="store_true", help="save what's read now as the baseline")
    args = parser.parse_args()
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() and not args.save_baseline else None
    saved = {}
    try:
        os.nice(15)
    except OSError:
        pass
    folders = sorted(p.parent for p in CAPTURES.glob("*/summary.jpg") if (p.parent / "stats.json").exists())
    if args.sample:
        folders = folders[-args.sample:]
    changed = 0
    for i, folder in enumerate(folders, 1):
        stored = baseline.get(folder.name) if baseline else json.loads((folder / "stats.json").read_text()).get("summary")
        image = cv2.imread(str(folder / "summary.jpg"))
        if not stored or image is None:
            continue
        now = Extract_Stats.parse_summary(image)
        if args.save_baseline:
            saved[folder.name] = now or {}
            continue
        diff = differences(stored, now)
        if diff:
            changed += 1
            print(f"{folder.name}: " + "; ".join(f"{what} {a!r} -> {b!r}" for what, a, b in diff), flush=True)
        if i % 25 == 0:
            print(f"  ... {i} of {len(folders)}", flush=True)
    if args.save_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(saved))
        print(f"Baseline: {len(saved)} summaries as read now -> {BASELINE}")
        return 0
    print(f"{len(folders)} summaries read again against {'the baseline' if baseline else 'stats.json'}: "
          f"{changed} read differently")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
