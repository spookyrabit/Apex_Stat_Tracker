"""
Put finished matches into the LibreOffice workbook (local, offline).

Each match goes into the next empty row of "Past Games" (only the data cells; the formula columns
are left to the workbook), and every player on the squad goes into "Squad Stats". LibreOffice
itself does the editing (see Calc_Macro.py), so dashboards and charts you build are never touched.

If the workbook is open in Calc, nothing is written (that would fight with your edits); the
matches wait and go in automatically once you close it. A match is only added once
(Captures/<match>/sheet.json remembers its row), and a match you typed in by hand is skipped.

Usage:
    python Sheet_Write.py                # add any matches that aren't in the workbook yet
    python Sheet_Write.py --convert      # one-time: build the .ods from the Google Sheets .xlsx
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------
# CONFIGURATION
# ---------------------------

HERE = Path(__file__).parent
WORKBOOK = HERE / "Apex Game Data S30.ods"
GOOGLE_EXPORT = HERE / "Apex Game Data S30.xlsx"
CAPTURES_FOLDER = HERE / "Captures"
PENDING_FLAG = CAPTURES_FOLDER / ".sheet_pending"   # matches waiting for the workbook to be closed
JOB_FILE = CAPTURES_FOLDER / ".calc_job.json"
RESULT_FILE = CAPTURES_FOLDER / ".calc_result.json"
LOG_FILE = CAPTURES_FOLDER / "libreoffice.log"
MACRO_SOURCE = HERE / "Calc_Macro.py"
# A private LibreOffice profile for the background jobs, so they never share (or get handed to)
# the LibreOffice you have open yourself
PROFILE = CAPTURES_FOLDER / ".libreoffice_profile"
MACRO_NAME = "apex_stats.py"
JOB_TIMEOUT = 300              # seconds

KEEP_SHEETS = ["Past Games", "Ranked RP Info", "Ring Info", "Legend Classes", "Gun Data", "Worlds Edge",
               "E-District", "Olympus", "Storm Point", "Broken Moon", "Kings Canyon", "Copy of Map"]
# How each Squad Stats column is stored (s text, n number, d date-time, t duration, p percent)
SQUAD_KINDS = {"Time": "d", "Placement": "n", "Squad Kills": "n", "Entry": "n", "Dropship Heading": "n",
               "Flight Path": "n", "Slot": "n", "Kills": "n", "Assists": "n", "Knocks": "n", "Damage": "n",
               "Damage %": "p", "Survival Time": "t", "Revives": "n", "Respawns": "n"}

# Past Games cells this writes: column -> (index in Extract_Stats' row, kind). Death Zone, POI,
# Contested etc. aren't listed, so whatever you type there is never overwritten.
GAME_CELLS = {"A": (0, "n"), "B": (1, "n"), "C": (2, "n"), "D": (3, "n"), "E": (4, "n"), "F": (5, "t"),
              "G": (6, "n"), "H": (7, "n"), "I": (8, "n"), "J": (9, "s"), "L": (11, "p"), "O": (14, "s"),
              "P": (15, "s"), "Q": (16, "s"), "R": (17, "s"), "S": (18, "s"), "T": (19, "n"),
              "Y": (24, "n"), "AA": (26, "s"),
              # Worked out from the minimap / kill feed; only filled if you haven't typed something there
              "M": (12, "s?"), "U": (20, "s?"), "V": (21, "s?")}
ALWAYS_SET = {"T"}             # blank Entery matters to the RP formulas, so clear it when there's none

# ---------------------------
# LIBREOFFICE
# ---------------------------

def libreoffice():
    return shutil.which("libreoffice") or shutil.which("soffice") or "/snap/bin/libreoffice"

def install_macro():
    folder = PROFILE / "user/Scripts/python"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / MACRO_NAME
    source = MACRO_SOURCE.read_text().replace("@JOB_FILE@", str(JOB_FILE))
    if not target.exists() or target.read_text() != source:
        target.write_text(source)

def lock_file(path):
    return path.parent / f".~lock.{path.name}#"

def run_job(job):
    """Hand a job to the macro in a headless LibreOffice and wait for its result."""
    install_macro()
    job["result"] = str(RESULT_FILE)
    job["log"] = str(LOG_FILE)
    RESULT_FILE.unlink(missing_ok=True)
    with open(JOB_FILE, "w") as f:
        json.dump(job, f)
    url = f"vnd.sun.star.script:{MACRO_NAME}$run_job?language=Python&location=user"
    subprocess.run([libreoffice(), f"-env:UserInstallation={PROFILE.as_uri()}", "--headless", "--norestore", url],
                   timeout=JOB_TIMEOUT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + JOB_TIMEOUT
    while not RESULT_FILE.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("LibreOffice didn't finish the job")
        time.sleep(0.5)
    time.sleep(0.2)
    with open(RESULT_FILE) as f:
        result = json.load(f)
    JOB_FILE.unlink(missing_ok=True)
    RESULT_FILE.unlink(missing_ok=True)
    if not result.get("ok"):
        raise RuntimeError(result.get("error"))
    return result

# ---------------------------
# MATCH -> CELLS
# ---------------------------

def iso_time(text):
    """Extract_Stats writes "M/D/YYYY H:MM:SS"; the macro wants ISO."""
    return datetime.strptime(text, "%m/%d/%Y %H:%M:%S").isoformat() if text else ""

def squad_cells(squad_rows):
    """Extract_Stats' squad rows (dicts) -> [kind, value] cells in Squad Stats column order."""
    from Extract_Stats import SQUAD_HEADER
    out = []
    for row in squad_rows:
        row = dict(row, Time=iso_time(row["Time"]))
        if row["Survival Time"]:
            row["Survival Time"] = f"0:{row['Survival Time']}"      # "6:05" -> "0:6:05"
        out.append([[SQUAD_KINDS.get(col, "s"), row[col]] for col in SQUAD_HEADER])
    return out

def match_job(stats):
    """The macro's view of one match, or None if it's missing the basics."""
    from Extract_Stats import build_rows
    row, squad_rows = build_rows(stats)
    if not stats.get("ended") or row[0] == "":
        return None   # no placement: a row without Placement would look empty to the next write
    cells = []
    for col, (i, kind) in GAME_CELLS.items():
        if row[i] != "" or col in ALWAYS_SET:
            cells.append([col, kind, row[i]])
    cells.append(["K", "d", stats["ended"]])
    return {"match": stats["match"], "ended": stats["ended"], "placement": row[0], "damage": row[8],
            "cells": cells, "squad_rows": squad_cells(squad_rows)}

# ---------------------------
# MAIN SCRIPT
# ---------------------------

def write_pending(all_stats):
    """Add matches that aren't in the workbook yet. `all_stats`: stats.json dicts from Extract_Stats."""
    if not WORKBOOK.exists():
        print(f"No {WORKBOOK.name} yet - run: python Sheet_Write.py --convert")
        return
    jobs = []
    for stats in all_stats:
        if (CAPTURES_FOLDER / stats["match"] / "sheet.json").exists():
            continue
        job = match_job(stats)
        if job is None:
            print(f"  {stats['match']}: not added, no placement - type it in by hand")
            continue
        jobs.append(job)
    if not jobs:
        PENDING_FLAG.unlink(missing_ok=True)
        return
    if lock_file(WORKBOOK).exists():
        print(f"  {WORKBOOK.name} is open in Calc; {len(jobs)} match(es) will be added when it's closed")
        PENDING_FLAG.touch()
        return
    result = run_job({"action": "append", "workbook": str(WORKBOOK), "matches": jobs})
    for r in result["matches"]:
        note = "already there (typed in by hand)" if r["already_there"] else "added"
        print(f"  {r['match']}: {note}, Past Games row {r['row']}")
        with open(CAPTURES_FOLDER / r["match"] / "sheet.json", "w") as f:
            json.dump({"row": r["row"], "already_there": r["already_there"],
                       "written": datetime.now().isoformat(timespec="seconds")}, f, indent=2)
    PENDING_FLAG.unlink(missing_ok=True)

def remove(match_names):
    if lock_file(WORKBOOK).exists():
        sys.exit(f"Close {WORKBOOK.name} in Calc first")
    matches = []
    for name in match_names:
        folder = CAPTURES_FOLDER / name
        marker = json.loads((folder / "sheet.json").read_text()) if (folder / "sheet.json").exists() else {}
        stats = json.loads((folder / "stats.json").read_text())
        # A row typed in by hand stays put; only a row this script wrote gets cleared
        row = None if marker.get("already_there") else marker.get("row")
        matches.append({"match": name, "ended": stats.get("ended", ""), "row": row})
    columns = list(GAME_CELLS) + ["K"]
    for r in run_job({"action": "remove", "workbook": str(WORKBOOK), "matches": matches, "columns": columns})["matches"]:
        print(f"  {r['match']}: Past Games row {'cleared' if r['row_cleared'] else 'left alone'}, "
              f"{r['squad_rows_removed']} Squad Stats rows removed")
        (CAPTURES_FOLDER / r["match"] / "sheet.json").unlink(missing_ok=True)

def squad_header():
    from Extract_Stats import SQUAD_HEADER
    return SQUAD_HEADER

def rebuild_squad():
    """Start Squad Stats over from every captured match (after its columns change)."""
    from Extract_Stats import build_rows
    rows = []
    for stats in load_all_stats():
        rows += squad_cells(build_rows(stats)[1])
    result = run_job({"action": "rebuild_squad", "workbook": str(WORKBOOK),
                      "squad_header": squad_header(), "squad_rows": rows})
    print(f"Squad Stats rebuilt: {result['rows']} player rows")

def refresh():
    """Rewrite the data cells of rows this script wrote, with what the matches read as now (after
    fixes to the reading). Rows you typed in by hand are left alone, empty readings never blank a cell,
    and Death Zone / POI / Contested are still only filled where empty."""
    updates = []
    for stats in load_all_stats():
        marker = CAPTURES_FOLDER / stats["match"] / "sheet.json"
        if not marker.exists() or json.loads(marker.read_text()).get("already_there"):
            continue
        job = match_job(stats)
        if job:
            cells = [c for c in job["cells"] if c[2] != ""]
            updates.append({"row": json.loads(marker.read_text())["row"], "cells": cells})
    if updates:
        run_job({"action": "update_rows", "workbook": str(WORKBOOK), "updates": updates})
    print(f"Refreshed {len(updates)} rows")

def fill_in():
    """Fill Death Zone / POI / Contested on rows already written, where they're still empty."""
    from Extract_Stats import build_rows
    updates = []
    for stats in load_all_stats():
        marker = CAPTURES_FOLDER / stats["match"] / "sheet.json"
        if not marker.exists():
            continue
        row = build_rows(stats)[0]
        cells = [[col, GAME_CELLS[col][1], row[GAME_CELLS[col][0]]] for col in ("M", "U", "V")
                 if row[GAME_CELLS[col][0]] != ""]
        if cells:
            updates.append({"row": json.loads(marker.read_text())["row"], "cells": cells})
    if updates:
        run_job({"action": "update_rows", "workbook": str(WORKBOOK), "updates": updates})
    for u in updates:
        print(f"  row {u['row']}: " + ", ".join(f"{c}={v}" for c, _, v in u["cells"]) + " (only where empty)")

def load_all_stats():
    out = []
    for path in sorted(CAPTURES_FOLDER.glob("*/stats.json")):
        with open(path) as f:
            stats = json.load(f)
        if stats.get("summary") or (stats.get("location") or {}).get("squads_left_at_end"):
            out.append(stats)
    return out

def convert():
    """One-time: Google Sheets export -> LibreOffice workbook, keeping the data and lookup tabs."""
    import openpyxl
    if WORKBOOK.exists():
        sys.exit(f"{WORKBOOK.name} already exists - delete it first if you really want to rebuild it")
    cached = openpyxl.load_workbook(GOOGLE_EXPORT, data_only=True)["Gun Data"]
    gun_types = {f"M{r}": cached[f"M{r}"].value for r in list(range(2, 9)) + list(range(13, 19))}
    # Squad rows for matches captured before the workbook existed; their Past Games rows were typed by hand
    from Extract_Stats import build_rows
    squad_rows = []
    for stats in load_all_stats():
        squad_rows += squad_cells(build_rows(stats)[1])
    checks = ["Past Games!Z2", "Past Games!AB2", "Past Games!AC2", "Past Games!AH794", "Past Games!AG2",
              "Ranked RP Info!G4", "Ranked RP Info!H4", "Ranked RP Info!I5", "Ranked RP Info!Q2",
              "Gun Data!N2", "Gun Data!O14", "Gun Data!P2"]
    # Work from a copy: if the .xlsx is open in Calc its lock would stall the headless LibreOffice
    source = CAPTURES_FOLDER / ".convert_source.xlsx"
    shutil.copyfile(GOOGLE_EXPORT, source)
    try:
        result = run_job({"action": "convert", "source": str(source), "target": str(WORKBOOK),
                          "keep": KEEP_SHEETS, "gun_types": gun_types, "squad_header": squad_header(),
                          "squad_rows": squad_rows, "checks": checks})
    finally:
        source.unlink(missing_ok=True)
    print(f"Built {WORKBOOK.name} (last game on row {result['last_game_row']})")
    for cell, value in result["checks"].items():
        print(f"  {cell} = {value}")

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--convert", action="store_true", help="build the .ods from the Google Sheets .xlsx")
    parser.add_argument("--unhide", action="store_true", help="show every hidden sheet in the workbook")
    parser.add_argument("--remove", nargs="+", metavar="MATCH", help="take matches (Captures folder names) back out")
    parser.add_argument("--rebuild-squad", action="store_true", help="start Squad Stats over from every captured match")
    parser.add_argument("--fill-in", action="store_true", help="fill Death Zone / POI / Contested on rows already written")
    parser.add_argument("--refresh", action="store_true", help="rewrite rows this script wrote with the latest readings")
    args = parser.parse_args()
    if (args.rebuild_squad or args.fill_in or args.remove or args.refresh) and lock_file(WORKBOOK).exists():
        sys.exit(f"Close {WORKBOOK.name} in Calc first")
    if args.convert:
        convert()
    elif args.rebuild_squad or args.fill_in or args.refresh:
        if args.refresh:
            refresh()
        if args.rebuild_squad:
            rebuild_squad()
        if args.fill_in:
            fill_in()
    elif args.remove:
        remove(args.remove)
    elif args.unhide:
        if lock_file(WORKBOOK).exists():
            sys.exit(f"Close {WORKBOOK.name} in Calc first")
        result = run_job({"action": "unhide", "workbook": str(WORKBOOK)})
        print(f"Unhid: {', '.join(result['unhidden']) or 'nothing (no hidden sheets)'}")
        print(f"Sheets: {', '.join(result['sheets'])}")
    else:
        write_pending(load_all_stats())

if __name__ == "__main__":
    main()
