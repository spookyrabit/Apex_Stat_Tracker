"""
Runs INSIDE LibreOffice (as a Python macro), so LibreOffice itself edits the workbook and nothing
else in it (charts, dashboards, formatting) is touched. Sheet_Write.py installs this file into
LibreOffice's user Scripts folder and starts it headless; the job to do is passed in a JSON file.

Jobs:
    convert  - one-time: the Google Sheets export (.xlsx) -> a LibreOffice workbook (.ods)
    append   - add finished matches to "Past Games" and every player to "Squad Stats"
    new_season - a copy of last season's workbook with its games taken out
"""
import json
import traceback
from datetime import datetime, timedelta

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.lang import Locale

JOB_FILE = "@JOB_FILE@"        # filled in by Sheet_Write.py when it installs this macro

GAMES = "Past Games"
SQUAD = "Squad Stats"
LAST_FORMULA_ROW = 5000        # formula columns are filled down this far, ready for new games
DATA_END = 5000                # how far down the dashboard formulas look
SAME_MATCH_MINUTES = 10
SHEET_EPOCH = datetime(1899, 12, 30)

# LibreOffice versions of the "Past Games" formula columns ({r} = row)
GAME_FORMULAS = {
    "Z": '=(C{r}*50)+((F{r}*60*60*24)*3)+(I{r}/4)+(G{r}*25)+(H{r}*200)+(IF(A{r}="";0;(IF(A{r}<=5;300;0))))',
    "AB": '=IF(AA{r}="";"";(2*VALUE(REGEX(AA{r};"\\d+";;1))+VALUE(REGEX(AA{r};"\\d+";;2))+4*VALUE(REGEX(AA{r};"\\d+";;3)))/7)',
    "AC": '=IF(I{r}="";"";TEXTJOIN("-";1;SORT(TOCOL(HSTACK(J{r};R{r};S{r})))))',
    "AD": "=XLOOKUP(A{r};$'Ranked RP Info'.$A$2:$A$21;$'Ranked RP Info'.$C$2:$C$21)*(C{r}+D{r})",
    "AE": "=XLOOKUP(A{r};$'Ranked RP Info'.$A$2:$A$21;$'Ranked RP Info'.$B$2:$B$21)",
    "AF": "=((B{r}-(D{r}+C{r}))/2)*XLOOKUP(A{r};$'Ranked RP Info'.$A$2:$A$21;$'Ranked RP Info'.$C$2:$C$21)",
    "AG": "=AD{r}+AE{r}+AF{r}-T{r}",
    "AH": '=IF(A{r}="";"";(IF(O{r}="Ranked";(IF(AG{r}>0;(IF(S{r}="Empty";0;AG{r}));AG{r}));"")))',
}

# Ranked RP Info: Google FILTER takes several conditions, LibreOffice's takes one (multiply them)
PG = "$'Past Games'."
def ranked_filter(col, r):
    return (f"FILTER({PG}{col}$2:{col}${DATA_END};({PG}T$2:T${DATA_END}=F{r})*({PG}A$2:A${DATA_END}<>\"\"))")
RANKED_FORMULAS = {
    "G": lambda r: f'=IFERROR(AVERAGE({ranked_filter("AH", r)});"")',
    "H": lambda r: f'=IFERROR(COUNT({ranked_filter("AH", r)});0)',
    "I": lambda r: f'=IFERROR(AVERAGE({ranked_filter("L", r)});"")',
}
def ranked_kills(r):
    a = f"{PG}$A$2:$A${DATA_END}=J{r}"
    return (f"=IFERROR(ROUND(AVERAGE(FILTER({PG}$C$2:$C${DATA_END};{a}))"
            f"+AVERAGE(FILTER({PG}$D$2:$D${DATA_END};{a})));\"\")")

# ---------------------------
# HELPERS
# ---------------------------

def log(job, message):
    """Progress notes, so a slow or stuck job can be diagnosed."""
    with open(job["log"], "a") as f:
        f.write(f"{datetime.now():%H:%M:%S} {job['action']}: {message}\n")

def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p

def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1

def number_format(doc, code):
    formats = doc.NumberFormats
    locale = Locale("en", "US", "")
    key = formats.queryKey(code, locale, False)
    return key if key != -1 else formats.addNew(code, locale)

def serial(dt):
    return (dt - SHEET_EPOCH).total_seconds() / 86400

def set_cell(doc, cell, kind, value):
    """kind: n number, s text, t duration "h:mm:ss", d datetime ISO, p percent (0.25 = 25%), None = clear.
    A trailing "?" (e.g. "s?") only fills the cell if it's empty, so nothing you typed is overwritten."""
    if kind and kind.endswith("?"):
        if cell.getType().value != "EMPTY":
            return
        kind = kind[:-1]
    if kind is None or value in (None, ""):
        cell.clearContents(1 | 2 | 4 | 16)          # value, datetime, string, formula; keep formatting
        return
    if kind == "s":
        cell.setString(str(value))
        return
    if kind == "t":
        h, m, s = (int(x) for x in str(value).split(":"))
        cell.setValue((h * 3600 + m * 60 + s) / 86400)
        if cell.NumberFormat == 0:
            cell.NumberFormat = number_format(doc, "[H]:MM:SS")
        return
    if kind == "d":
        cell.setValue(serial(datetime.fromisoformat(value)))
        if cell.NumberFormat == 0:
            cell.NumberFormat = number_format(doc, "M/D/YYYY H:MM:SS")
        return
    cell.setValue(float(value))
    if kind == "p":                               # always shown as a percentage (the column held -2..2 before)
        cell.NumberFormat = number_format(doc, "0.0%")

def last_used_row(sheet, col, limit=LAST_FORMULA_ROW):
    """1-based number of the last row with something in `col` (0 if none)."""
    data = sheet.getCellRangeByName(f"{col}1:{col}{limit}").getDataArray()
    for i in range(len(data) - 1, -1, -1):
        if data[i][0] not in ("", None):
            return i + 1
    return 0

def fill_game_formulas(sheet, first, last):
    for col, template in GAME_FORMULAS.items():
        block = sheet.getCellRangeByName(f"{col}{first}:{col}{last}")
        block.setFormulaArray(tuple((template.format(r=r),) for r in range(first, last + 1)))

def open_doc(desktop, path):
    return desktop.loadComponentFromURL(uno.systemPathToFileUrl(path), "_blank", 0, (prop("Hidden", True),))

# ---------------------------
# JOBS
# ---------------------------

def convert(desktop, job):
    log(job, "loading")
    doc = open_doc(desktop, job["source"])
    doc.enableAutomaticCalculation(False)
    sheets = doc.Sheets
    for name in list(sheets.ElementNames):
        if name not in job["keep"]:
            log(job, f"removing {name}")
            sheets.removeByName(name)

    games = sheets.getByName(GAMES)
    last_game = last_used_row(games, "A")
    log(job, "past games formulas")
    fill_game_formulas(games, 2, LAST_FORMULA_ROW)
    # Rows below the last game: drop the old manual-entry templates (=NOW(), pre-typed legend/map...)
    for col in ("J", "K", "O", "P", "T"):
        games.getCellRangeByName(f"{col}{last_game + 1}:{col}{LAST_FORMULA_ROW}").clearContents(1 | 2 | 4 | 16)
    games.getCellRangeByName("AI1:AK1").clearContents(1 | 2 | 4 | 16)   # pointed at a deleted dashboard

    log(job, "ranked / guns / squad")
    ranked = sheets.getByName("Ranked RP Info")
    for col, make in RANKED_FORMULAS.items():
        for r in range(2, 9):
            ranked.getCellRangeByName(f"{col}{r}").setFormula(make(r))
    for r in range(2, 22):
        ranked.getCellRangeByName(f"Q{r}").setFormula(ranked_kills(r))

    guns = sheets.getByName("Gun Data")
    for a1, value in job["gun_types"].items():      # UNIQUE() doesn't spill in Calc: keep the lists as values
        guns.getCellRangeByName(a1).setString(value)
    for r in range(2, 9):
        guns.getCellRangeByName(f"N{r}").setFormula(f"=SUMIF($B$2:$B$30;$M{r};F$2:F$30)")
        guns.getCellRangeByName(f"O{r}").setFormula(f"=SUMIF($B$2:$B$30;$M{r};G$2:G$30)")
    for r in range(13, 19):
        guns.getCellRangeByName(f"N{r}").setFormula(f"=SUMIF($C$2:$C$30;$M{r};F$2:F$30)")
        guns.getCellRangeByName(f"O{r}").setFormula(f"=SUMIF($C$2:$C$30;$M{r};G$2:G$30)")

    sheets.insertNewByName(SQUAD, 1)
    squad = sheets.getByName(SQUAD)
    write_squad_header(doc, squad, job["squad_header"])
    append_squad(doc, squad, job["squad_rows"])

    log(job, "recalculating")
    doc.enableAutomaticCalculation(True)
    doc.calculateAll()
    checks = {a1: sheets.getByName(s).getCellRangeByName(a1.split("!")[1]).getString()
              for a1, s in ((c, c.split("!")[0]) for c in job.get("checks", []))}
    log(job, "saving")
    doc.storeToURL(uno.systemPathToFileUrl(job["target"]), (prop("FilterName", "calc8"),))
    doc.close(True)
    return {"last_game_row": last_game, "checks": checks}

def append_squad(doc, squad, rows):
    start = last_used_row(squad, "A") + 1
    for i, row in enumerate(rows):
        for c, (kind, value) in enumerate(row):
            set_cell(doc, squad.getCellByPosition(c, start - 1 + i), kind, value)

def existing_games(games, last):
    """(row, placement, damage, time) for recent rows, to skip matches typed in by hand."""
    first = max(2, last - 60)
    data = games.getCellRangeByName(f"A{first}:K{last}").getDataArray()
    out = []
    for i, r in enumerate(data):
        if r[0] in ("", None) or not isinstance(r[10], float):
            continue
        out.append((first + i, r[0], r[8], SHEET_EPOCH + timedelta(days=r[10])))
    return out

def fill_headers(doc, games, job):
    """Row 1 headers for columns the scripts added (Sheet_Write.GAME_HEADERS), where the cell is empty."""
    for col, title in job.get("headers", {}).items():
        set_cell(doc, games.getCellByPosition(col_index(col), 0), "s?", title)

def append(desktop, job):
    log(job, "loading")
    doc = open_doc(desktop, job["workbook"])
    log(job, "loaded")
    sheets = doc.Sheets
    games = sheets.getByName(GAMES)
    fill_headers(doc, games, job)
    squad = sheets.getByName(SQUAD)
    results = []
    for match in job["matches"]:
        last = last_used_row(games, "A")
        ended = datetime.fromisoformat(match["ended"])
        row = None
        for number, placement, damage, when in existing_games(games, last):
            if (placement == match["placement"] and damage == match["damage"]
                    and abs(when - ended) <= timedelta(minutes=SAME_MATCH_MINUTES)):
                row = number
        duplicate = row is not None
        if not duplicate:
            row = last + 1
            if not games.getCellRangeByName(f"Z{row}").getFormula():
                fill_game_formulas(games, row, row)
            for col, kind, value in match["cells"]:
                set_cell(doc, games.getCellByPosition(col_index(col), row - 1), kind, value)
        append_squad(doc, squad, match["squad_rows"])
        results.append({"match": match["match"], "row": row, "already_there": duplicate})
    log(job, "saving")
    doc.store()
    doc.close(True)
    log(job, "done")
    return {"matches": results}

def write_squad_header(doc, squad, header):
    for i, title in enumerate(header):
        squad.getCellByPosition(i, 0).setString(title)
    squad.getRows().getByIndex(0).CharWeight = 150      # bold header

def rebuild_squad(desktop, job):
    """Start "Squad Stats" over with a new set of columns (the rows come from the captures)."""
    doc = open_doc(desktop, job["workbook"])
    sheets = doc.Sheets
    if sheets.hasByName(SQUAD):
        sheets.removeByName(SQUAD)
    sheets.insertNewByName(SQUAD, 1)
    squad = sheets.getByName(SQUAD)
    write_squad_header(doc, squad, job["squad_header"])
    append_squad(doc, squad, job["squad_rows"])
    doc.store()
    doc.close(True)
    return {"rows": len(job["squad_rows"])}

def update_rows(desktop, job):
    """Fill in cells on rows already written (e.g. POI / Death Zone worked out later)."""
    doc = open_doc(desktop, job["workbook"])
    games = doc.Sheets.getByName(GAMES)
    fill_headers(doc, games, job)
    for update in job["updates"]:
        for col, kind, value in update["cells"]:
            set_cell(doc, games.getCellByPosition(col_index(col), update["row"] - 1), kind, value)
    doc.store()
    doc.close(True)
    return {"rows": [u["row"] for u in job["updates"]]}

def update_matches(desktop, job):
    """Rewrite matches after a correction: cells on their Past Games rows, and their Squad Stats rows
    replaced where they are (or added at the end if there were none)."""
    doc = open_doc(desktop, job["workbook"])
    games = doc.Sheets.getByName(GAMES)
    squad = doc.Sheets.getByName(SQUAD)
    fill_headers(doc, games, job)
    if job.get("squad_header"):                   # the rows may have a column the header doesn't have yet (Rank)
        write_squad_header(doc, squad, job["squad_header"])
    for update in job["updates"]:
        for col, kind, value in update["cells"]:
            set_cell(doc, games.getCellByPosition(col_index(col), update["row"] - 1), kind, value)
    out = []
    for match in job["squads"]:
        names = squad.getCellRangeByName(f"A1:A{last_used_row(squad, 'A')}").getDataArray()
        old = [i for i, (name,) in enumerate(names) if name == match["match"]]
        for i in reversed(old):
            squad.getRows().removeByIndex(i, 1)
        start = old[0] if old else last_used_row(squad, "A")
        squad.getRows().insertByIndex(start, len(match["squad_rows"]))
        for i, row in enumerate(match["squad_rows"]):
            for c, (kind, value) in enumerate(row):
                set_cell(doc, squad.getCellByPosition(c, start + i), kind, value)
        out.append({"match": match["match"], "squad_rows": len(match["squad_rows"]), "replaced": len(old)})
    doc.store()
    doc.close(True)
    return {"rows": [u["row"] for u in job["updates"]], "squads": out}

def map_assets(desktop, job):
    """Where each POI name typed on a map tab sits over that tab's map image (0-1 across the image),
    and optionally take the images out of the workbook."""
    doc = open_doc(desktop, job["workbook"])
    out = {}
    for name in job["sheets"]:
        sheet = doc.Sheets.getByName(name)
        page = sheet.DrawPage
        pictures = [page.getByIndex(i) for i in range(page.Count)
                    if page.getByIndex(i).supportsService("com.sun.star.drawing.GraphicObjectShape")]
        if not pictures:
            out[name] = None
            if job.get("remove_images"):
                for shape in [page.getByIndex(i) for i in range(page.Count)]:
                    page.remove(shape)
            continue
        picture = max(pictures, key=lambda shape: shape.Size.Width * shape.Size.Height)
        px, py = picture.Position.X, picture.Position.Y
        pw, ph = picture.Size.Width, picture.Size.Height
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        end = cursor.getRangeAddress()
        data = sheet.getCellRangeByPosition(0, 0, end.EndColumn, end.EndRow).getDataArray()
        labels = []
        for r, row in enumerate(data):
            for c, value in enumerate(row):
                if isinstance(value, str) and value.strip():
                    cell = sheet.getCellByPosition(c, r)
                    cx = cell.Position.X + cell.Size.Width / 2
                    cy = cell.Position.Y + cell.Size.Height / 2
                    labels.append([value.strip(), (cx - px) / pw, (cy - py) / ph])
        out[name] = {"labels": labels}
        if job.get("remove_images"):
            # The map tabs hold nothing but the map picture, so clear every drawing on them
            for shape in [page.getByIndex(i) for i in range(page.Count)]:
                page.remove(shape)
    if job.get("remove_images"):
        doc.store()
    doc.close(True)
    return {"maps": out}

def remove(desktop, job):
    """Take a match back out: clear its Past Games data cells, delete its Squad Stats rows."""
    doc = open_doc(desktop, job["workbook"])
    games = doc.Sheets.getByName(GAMES)
    squad = doc.Sheets.getByName(SQUAD)
    out = []
    for match in job["matches"]:
        row = match.get("row")
        cleared = False
        if row:
            when = games.getCellRangeByName(f"K{row}").getValue()
            # Only clear the row if it's still this match (same end time), never someone else's row
            if abs(SHEET_EPOCH + timedelta(days=when) - datetime.fromisoformat(match["ended"])) < timedelta(seconds=2):
                for col in job["columns"]:
                    games.getCellRangeByName(f"{col}{row}").clearContents(1 | 2 | 4 | 16)
                cleared = True
        names = squad.getCellRangeByName(f"A1:A{last_used_row(squad, 'A')}").getDataArray()
        doomed = [i for i, (name,) in enumerate(names) if name == match["match"]]
        for i in reversed(doomed):
            squad.getRows().removeByIndex(i, 1)
        out.append({"match": match["match"], "row_cleared": cleared, "squad_rows_removed": len(doomed)})
    doc.store()
    doc.close(True)
    return {"matches": out}

def new_season(desktop, job):
    """A workbook for a new season: a copy of the last one with its games taken out. The formula columns,
    the lookup tabs (Ranked RP Info, Ring Info, Legend Classes) and the formatting stay."""
    doc = open_doc(desktop, job["source"])
    games = doc.Sheets.getByName(GAMES)
    played = last_used_row(games, "A") - 1
    for c in range(col_index("AZ") + 1):
        letters = (chr(64 + c // 26) if c >= 26 else "") + chr(65 + c % 26)
        if letters not in GAME_FORMULAS:
            games.getCellRangeByName(f"{letters}2:{letters}{LAST_FORMULA_ROW}").clearContents(1 | 2 | 4 | 16)
    squad = doc.Sheets.getByName(SQUAD)
    players = last_used_row(squad, "A") - 1
    if players > 0:
        squad.getRows().removeByIndex(1, players)
    doc.calculateAll()
    doc.storeToURL(uno.systemPathToFileUrl(job["target"]), (prop("FilterName", "calc8"),))
    doc.close(True)
    return {"games_removed": played, "squad_rows_removed": players}

def unhide(desktop, job):
    """Make every hidden sheet visible again."""
    doc = open_doc(desktop, job["workbook"])
    shown, names = [], list(doc.Sheets.ElementNames)
    for name in names:
        sheet = doc.Sheets.getByName(name)
        if not sheet.IsVisible:
            sheet.IsVisible = True
            shown.append(name)
    if shown:
        doc.store()
    doc.close(True)
    return {"unhidden": shown, "sheets": names}

def inspect(desktop, job):
    """What cells show and their number formats, for checking a workbook without opening it."""
    doc = open_doc(desktop, job["workbook"])
    formats = doc.NumberFormats
    out = {}
    for ref in job["cells"]:
        sheet, a1 = ref.split("!")
        cell = doc.Sheets.getByName(sheet).getCellRangeByName(a1)
        out[ref] = [cell.getString(), formats.getByKey(cell.NumberFormat).FormatString, cell.getFormula()]
    doc.close(True)
    return {"cells": out}

# ---------------------------
# ENTRY POINT
# ---------------------------

JOBS = {"convert": convert, "append": append, "update_rows": update_rows, "update_matches": update_matches,
        "rebuild_squad": rebuild_squad, "new_season": new_season,
        "remove": remove, "map_assets": map_assets, "unhide": unhide, "inspect": inspect}

def run_job(*args):
    desktop = XSCRIPTCONTEXT.getDesktop()
    with open(JOB_FILE) as f:
        job = json.load(f)
    try:
        result = {"ok": True, **JOBS[job["action"]](desktop, job)}
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    with open(job["result"], "w") as f:
        json.dump(result, f, indent=2)
    # Started just for this job? Shut down. Your own open Calc windows are left alone.
    if not desktop.getComponents().createEnumeration().hasMoreElements():
        desktop.terminate()

g_exportedScripts = (run_job,)
