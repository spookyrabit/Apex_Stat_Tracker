# Apex Stat Tracker

Records Apex Legends stats automatically while you play on PS5 through a capture card in OBS. A
lightweight background service watches the screen, keeps only the screenshots that hold data, reads the
stats out of them after each match, and writes everything into a local LibreOffice workbook. It also works
out where everyone in your squad went on the map, where the rings were, and draws a picture of each match
(live in OBS while you play, and a more accurate final version afterwards).

Nothing is sent to the game: it only looks at the screen.

```text
OBS "Capture Card" source
   │  WebSocket screenshot, 3x a second (small) + full size only when it's worth keeping
   ▼
Stat_Capture.py  ── recognises the screen ─► saves screenshots / crops into Captures/<match>/
   │  (live) Match_Map.LiveMap ─► "Latest Match Map.png" + "... Zoomed.png" every 5 s (for OBS)
   │  after the stats screen:
   ▼
Extract_Stats.py ── reads the screenshots ─► stats.json
   ├─ Map_Locate.py ─ minimaps → positions → paths, POI, Death Zone, Contested, rings, scans
   │     └─ Match_Map.py ─ match picture, zoomed picture, match_paths.svg, trails.json
   ├─ Sheet_Write.py + Calc_Macro.py ─► "Apex Game Data S<season>.ods" (Past Games + Squad Stats)
   ├─ clean_up() ─ deletes the screenshots it no longer needs
   └─ Heatmap.py ─► Maps/<Map>/layer_heatmap.png, layer_rings.png (+ Ring_Predict.py ─► layer_land.png)
```

---

## Setup (once)

1. Python venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
   (OCR is RapidOCR, pip only; no Tesseract needed).
2. OBS (Flatpak): Tools → WebSocket Server Settings → Enable (port 4455), then **restart OBS** (it only
   started listening after a restart). The password is read straight from OBS's config file, nothing to copy.
   The OBS source must be named **Capture Card** (or set `{"source": "..."}` in `obs_settings.json`).
3. LibreOffice is the snap at `/snap/bin/libreoffice`. Background jobs use their own profile in
   `Captures/.libreoffice_profile`, so they never touch your normal LibreOffice.
4. In `Extract_Stats.py`: `PLAYER_NAME = "Spookyrabit"` (your tag) and `FRIENDS = {"tag": "Lobster"}` for
   the Friends column.
5. Game settings: minimap rotation **off**, default HUD scale, 1080p output from OBS.

## Running

### Quick start (every time, e.g. after a reboot)

1. Open a terminal in the project folder:
   `cd ~/Documents/Coding/Apex_Stat_Tracker`
2. Open the Control Panel (the window with your stats and the capture controls):
   `.venv/bin/python Control_Panel.py`
   (to keep it open after closing the terminal: `setsid nohup .venv/bin/python Control_Panel.py >/dev/null 2>&1 &`)
3. Start the capture, if it isn't running: in the panel's **Capture** tab press **Restart now**, or run
   `./restart_capture.sh` in the terminal. It doesn't start by itself after a reboot.
4. Open OBS with the **Capture Card** source showing, and play.

### Details

The capture runs as a systemd **user** service (a transient unit; it does not start by itself after a
reboot / log-in, run the restart script once):

| Command | What it does |
|---|---|
| `./restart_capture.sh` | start / restart the capture now |
| `./restart_capture.sh --after-match` | restart once the current match is in the workbook (use after code changes so a live match isn't cut) |
| `systemctl --user status apex-stat-capture` | is it running? |
| `tail -f Captures/capture.log` | what it's seeing (screens, matches, reading, workbook writes) |
| `Captures/libreoffice.log` | workbook jobs |

**The window:** `.venv/bin/python Control_Panel.py`. Filters along the top (season, mode, map, legend, period) apply to
every tab but Latest Match; the window is dark (colours at the top of `Control_Panel.py`). *Latest Match* (opens
first, to leave on screen while you play): the newest match folder's `Latest Match Map.png` big (Whole map /
Zoomed, picked up within 2 s of each redraw; until the live map has drawn this match - it needs the dropship's
minimaps - the match's newest whole screenshot instead: legend select, squad intro, dropship, the map), a status dot (live + current screen + time since the jump, reading
the stats, finished) and underneath the squad: mid-match who picked what (legend select read by
`Extract_Stats.read_legend_select` in a `nice` process 10 s after its last screenshot, again if a late pick
reveal comes in), then once the stats screen is read everyone's stats from `build_rows` (fixes included) - straight away from `stats_quick.json`, which the read writes before the minutes of map positions (status "Stats read · working out the map positions..."), then from stats.json - placement,
POI, hot drop, death zone and the RP from the workbook. Buttons above the picture switch it to a screen with what was read on it drawn over it (`Screen_Notes.py`, made in a `nice` process the first time you pick it, cached in `Captures/.notes/<match>/`, plain screenshot meanwhile): **Lobby** (Trios / Duos: the skill graph's track, band start / end, your team line, the Percentile and Skill they make; Ranked: the Squad Rank Distribution bar cut into its ranks, each with its squads, whether the count was read and the bar colour), **Legend select** (every OCR box: green legend, blue name, yellow mode / map, grey unused; the slots and picks), **Dropship** (compass and entry cost close up, the compass read every second, the heading used), **Map shot** (ring / next ring with round and metres, flight line, scans, squad arrows found on it), **Stats screen** (each card's value boxes and what was read), and the newest screenshot. **Endgame guess** (`Ring_Predict.py`): where the last ring is likely to end up, from the rings seen so far - mid-match from `Captures/live_rings.json` (the live map writes its rings there when they change), from trails.json once read; remade in a `nice` process when the rings change. Measured on your games: every next ring touches the edge of the one before (moves 90-100% of the room, radius minus next radius), the direction carries on from the last move (54% of turns under 45 deg, random 25%) and round 1 already leans away from the map's middle (8% of round 1 -> 2 moves head back to the middle), the last two rings form where players can go: playable ground from your paths alone (on foot only: skydives / jump towers / balloons over mountains left out) - where `PLAY_QUIET` (8)+ games walked within `LAND_RADIUS` (5% of the map) only ground within `PLAY_NEAR` (0.6% of the map's width) of a path, so a mountain or wall your squads go round is out; where fewer did it's unknown and all counted playable (up to `QUIET_REACH`, 3%, from a path: not the sea) - and a ring is possible when 70%+ of it is on playable ground (`RING_ON_LAND`), elsewhere 0.5% as likely (`OFF_LAND`). 44 of the 46 real last / second-last rings (96%) pass, each left out of its own land; the Maps tab's **Ring land** shows it, and the circles are picked from the shading so they line up with it: orange = the next ring holding the most of the last ring's chance, yellow = the brightest shading inside orange (picked from the play-outs, it sometimes sat off the bright part). 4000 play-outs of the rounds to come; shading = where the last ring's centre ends up, white line = half the chance, yellow = most likely last ring, orange = most likely next ring, plus the POIs it's nearest to. Back-tested (`.venv/bin/python Ring_Predict.py --backtest`, each game left out of its own land mask; 2026-11-01: 109 games past round 1, 72 past round 2): the white line held the deepest ring seen 58% of the time given round 1 (~2.3% of the map), 50% given rounds 1-2 (~0.7%) - close to the 50% it should, after widening the turns (`KAPPA` 2.5 -> 1.5, move spread 0.06 -> 0.12: before, 44% / 39%, too sure; log score ~1.3x / 1.4x better). Tried and dropped: weighting by where other games' rings of each round were (worse at every strength). *No win chance per landing spot*: 14 wins in 259 games, no POI has more than 2, and top 5 doesn't differ between POIs more than chance would (shuffle test p = 0.06 Olympus, 0.30 Worlds Edge, 0.88 Broken Moon); landing near where the endgame ends up doesn't place better either (top 5 72-89% at every distance). A live win chance would need SQUADS LEFT over time, which isn't kept (only at the end). **Ring-safe route** (green, from you - live: `me` in live_rings.json; a read match: your last spot - to the most likely last ring): the cheapest way across a 5 px grid where ground squads have walked (Heatmap's trails) costs 1-1.6x (busiest corridors cheapest), never-walked land 75x, off the land 150x, +100 outside the smallest ring seen (storm), +3 x the chance the next ring (not seen yet) leaves a spot out. On the 16:46 win from round 1 it took the same corridor the squad did (Fragment to Sorting Factory). *Not* a danger map: where your squad got knocked per minute spent there was tested leave-one-out and doesn't predict where you go down (x1.00, 41-58% of games, any smoothing) - fights follow the ring and other squads, not places. `.venv/bin/python Ring_Predict.py Captures/<match> out.jpg` for any read match. **Follow match** (on by default) switches to the newest stage by itself: lobby → Lobby, legend select / squad intro → Legend select, dropship → Dropship, in game → Live map, stats screen → Stats screen (Map shot and the newest screenshot only by hand); mid-match, when the live map finds a ring it hadn't had (live_rings.json gets another one: usually the next ring, once it's shown), the endgame guess is made with it and shown for `GUESS_SHOW` (8) s once it's ready, then back to the live map; picking a view yourself turns it off. Screens are only there until the match's clean-up, 20 min after it's read. *Overview*: games, wins, top 5, placement, kills / assists / knocks / damage per game, survival,
revives, share of squad damage, ranked RP, hot drops, best games, plus a breakdown table (group by legend, map,
mode, landing POI, death zone, teammate, squad, friends, weekday, hour, month; click a heading to sort).
*Squad*: the same from Squad Stats (every player, captured games only), for everyone / you / teammates / friends /
randoms: per-game stats, share of squad damage, how often each had the squad's top damage, squad damage per match,
most played with, best teammate (3+ games), plus a breakdown by player, you / friends / randoms, played with, legend,
squad, map, POI, own death zone... (hide groups under N games). Spellings of one tag (the end card cuts names off,
OCR slips letters) are merged with `Extract_Stats.name_similarity`; friends come from `FRIENDS`. *Matches*: every game in the workbook; pick one for its squad, match picture and stats screen. **Missing legends
only** lists captured games with a legend nobody could read (legend select not seen); **Set legends…** shows the
stats screen with a drop-down per player (a player whose name wasn't read is saved as `"#<card number>"`): Save writes `fixes.json` and rewrites that match in the workbook (close
Calc first); Save with nothing changed writes the saved legends again, so a workbook write that didn't go through can be redone. *Maps*: each map
with any of its layers stacked on it - **Ring land** (the endgame guess's masks), **Heatmap**, **Endgame rings**, **POI
names** - over the icon-free map (`reference_clean.png`, darkened `MAP_DIM`), with that map's stats; the stacked picture is
saved to `Captures/.notes/maps/<Map>.png` (click it to open full size). A new layer: draw it with `Heatmap.Layer`, add it to
`MAP_LAYERS` in `Control_Panel.py`. *Capture*: service status, Restart now /
after match / Stop, the `Sheet_Write.py` commands, the latest match map and `capture.log` live. Stats are read
straight from every season's workbook plus `Past_Seasons/past_games.csv` (read-only, fine while Calc has them
open) and re-read when one changes; **Open workbook** opens this season's. (A dancing-
characters tab used to live here; it's in the git history, commit `8e7a69a`.)

The restart script runs `systemctl --user reset-failed` first: a service that crashed while stopping
stays "failed" and refuses to start again (this once stopped capture for an hour).
Run the `--after-match` variant itself under systemd so it survives:
`systemd-run --user --unit=apex-restart-later --working-directory="$PWD" "$PWD/restart_capture.sh" --after-match`
Stopping or crashing the service also kills a stats read it started (systemd stops everything in the unit), so
OBS stalling is never fatal: any OBS timeout, also on the full-size grab while saving, just reconnects. After a
restart the match in progress is picked up again (within 10 min), on a pre-game screen too - before, a restart
between legend select and the dropship split one match into two folders. Between matches, screens nothing
recognises are kept in `Captures/.unknown/` (one per 20 s, newest 40), to build references for new layouts from
(Ranked's lobby was learned that way: cut a new anchor's crop by hand - `Build_References.py` recuts every anchor
of a screen from the one frame).

**OBS sources:** Image sources pointing at `Latest Match Map.png` and `Latest Match Map Zoomed.png` in this
folder (OBS reloads them when they change). The file always shows the newest match: live during a match,
then replaced by the final version 1-3 minutes after you stop spectating (or leave the stats screen).
The live map takes the first two shots of each time the map's opened, then one every `LIVE_MAP_SHOT_EVERY` (8) s
while it stays open (the capture still saves one every 2 s for the final read): each costs it ~3 s, and with the map up
half a minute it fell 20+ s behind (2026-11-03 19:34, the next ring never showed live). Its rings are drawn at their round's set size
(`set_size_rings`, `MapLocator.ring_size`, like the Maps tab's rings layer), and a ring that's no round's size - caught
closing, or the last ring closing to nothing - isn't drawn (nor put in `live_rings.json` for the endgame guess); a map whose
scale isn't known yet draws a ring of a size it has learned (within 12%) at that size, the rest as measured. Once a ring's
red edge has been seen on a map shot, only those sightings place it (`distinct_rings`): before that it's the next ring's lighter
disc, whose soft edge put Storm Point's round 2 ring 5 px off at its bottom edge, 2026-11-03 23:32 - big on the zoomed view
(~5x); the edge is within 1-2 px. A ring on a map shot whose ring timer says CLOSING is left out before
that, like the final read does (`rings_on_map_screen`): on a map with no sizes yet the closing edge got through, Storm Point's
"ROUND 1 - CLOSING" drawn as a 629 px ring, 2026-11-03 23:03. The finished picture, SVG and trails.json do the same (ring sizes are still learned from
the measured rings first).

---

## What you get

### The workbooks: one per season (`Apex Game Data S30.ods`, `Apex Game Data S31.ods`, ...)

Each match goes into the workbook of the season it was **played** in (`Seasons.py`: the season start times
are in `SEASON_STARTS`; S31 started 2026-11-03 13:00, 10 am Pacific), so a late match from the night before a
new season still goes into the old one. The first match of a new season makes its workbook by itself: a copy
of the last one with its games and Squad Stats rows taken out (formula columns, Ranked RP Info, Ring Info,
Legend Classes and the formatting stay). `Captures/<match>/sheet.json` names the workbook and row.

**A new season:** add its start to `SEASON_STARTS` in `Seasons.py` (e.g. `32: datetime(2027, 2, 9, 13, 0)`),
then optionally `.venv/bin/python Sheet_Write.py --new-season` to make the workbook straight away (otherwise
the season's first match does). If ranked RP rules change, update that workbook's **Ranked RP Info** tab.
Restart the capture after editing `Seasons.py`.

**Past seasons (S23-S29)** are your Google Sheets, downloaded as .xlsx into `Past_Seasons/`, cut down on 2026-11-03 to
just their Past Games tab as plain values (the dashboards and other tabs are still in the Google Sheets; the scripts never change these files).
`.venv/bin/python Past_Seasons.py` reads their Past Games (the columns moved between seasons, so they're found
by heading; spellings tidied: "KIngs Canyon", "Dues", Y / N, "Gibralter", "Newcastle" -> "New Castle"...) into `Past_Seasons/past_games.csv`,
which the Control Panel reads with the workbooks (Season filter at the top, Season column in Matches, Season
in the Overview breakdown). Run it again after adding or replacing a sheet. 6369 games: S23 1341, S24 746,
S25 1029, S26 409, S27 1198, S28 948, S29 698. They only have what you typed (no squads, match pictures or
heatmaps); S23's first 131 games have only a date (shown as "(no time typed)" by hour of day), and 9 S25 games
with no date at all get the date of the game next to them. S30's Google Sheet is there too, but S30 is read
from its .ods (it has the captured games). A few hand-typed games share their end time with the next one to the
millisecond (S30 rows 24-25 and 741-742, 8 in older seasons: a time stamp copied while typing); they're
different games, both counted. The capture can't do this: each match writes its own end time, and a game you
typed in yourself is skipped (same placement + damage within 10 min).

Written by headless LibreOffice after every match. **Never while the file is open in Calc**: the match
waits (`Captures/.sheet_pending`) and goes in within a minute of closing it. Matches you typed in by hand
are recognised (same placement + damage within 10 min) and skipped. Tabs: Past Games, Squad Stats,
Ranked RP Info (the RP formulas use it), Ring Info, Legend Classes.

**Past Games** - one row per match (next row with no Placement). Written: A Placement, B Squad Kills,
C Kills, D Assists, E Knocks, F Time, G Revives, H Respawns, I Damage, J Legend, K Time (end), L Most Damage?
(your share of squad damage, %), O Game Mode, P Map, Q Friends, R/S teammate legends ("Empty" = nobody in that
slot), T Entry (cleared when not ranked), W Rank Distribution (Ranked; header written in W1 if empty), Y Dropship
Heading, AA Percentile. **Only filled if the cell is
empty** (so your typing wins): M Death Zone, U POI, V Contested. Formula columns (Z EST XP, AB Skill, AC Squad,
AD-AH RP) are left to the workbook (filled down to row 5000).

**Squad Stats** - one row per player per match (filter Is Me = Yes for your own games): Match, Time, Game
Mode, Map, Placement, Squad Kills, Entry, Percentile, Dropship Heading, Flight Path, POI, Death Zone (each
player's own), Contested, Survey Scans, Stats Screen (Yes / Missed), Slot, Player, Is Me, Legend, Teammates,
Squad, Kills, Assists, Knocks, Damage, Damage %, Survival Time, Revives, Respawns, Rank (Ranked, S31 on: e.g. "Silver III").

`Captures/Sheet Rows.csv` and `Captures/Squad Stats.csv` hold the same rows (rebuilt every time).

| Command (`.venv/bin/python Sheet_Write.py ...`) | |
|---|---|
| *(no options)* | add matches not in the workbook yet |
| `--refresh` | rewrite rows the scripts wrote with the latest readings (never blanks a cell, never touches hand-typed rows) |
| `--rebuild-squad` | start Squad Stats over from every captured match |
| `--fill-in` | fill empty Death Zone / POI / Contested |
| `--remove <match folder name>` | take a match back out |
| `--update <match folder name>` | rewrite that match after a correction in its `fixes.json`: Past Games row (scripts' rows only; a correction overwrites, a blank never does) + its Squad Stats rows, in place |
| `--unhide` | show hidden tabs (this season's workbook) |
| `--new-season` | make the newest season's workbook now (`Seasons.SEASON_STARTS`) instead of at its first match |

Each works on every season's workbook (a match's rows are in its own season's). All of these refuse to run
while Calc has the file open. Back the workbooks up yourself (they're not in git).

### Per match: `Captures/<YYYY-MM-DD_HH-MM-SS>/`

While a match is running/being read every screenshot is kept (minimap every 1 s, kill feed every 2 s for
4 min after the jump, SQUADS LEFT and the squad panel every 5 s, compass every 1 s for the first 15 s, a full frame every 45 s,
every map open + every 2 s while it stays open, all end-of-match tabs, plus unrecognised screens right
after legend select and after the match). Spectating another squad after yours is out: only their minimap
(marked `after_squad`, used for the ring and nothing else) and the map if you open it; the match is read 15 s
after you stop spectating. A match with no readable stats screen counts as over once a newer one starts, or
once nothing has been saved for it for 30 minutes (`OVER_AFTER_MINUTES`; a game lasts up to ~20), so the last
game of a session isn't left unread. Once read and written, and at least 20 minutes later (`KEEP_FOR_MINUTES`,
so you can still look at / test them), `clean_up()` keeps only the files below. The capture checks once a minute
(`Extract_Stats.work_due()`, also while waiting for OBS, never mid-match) and starts a run for either, so
neither waits for the next match:

| File | |
|---|---|
| `stats.json` | everything read (summary cards, legend select, dropship, location: landing, death, squad_out, players_out, contested + evidence, rings, scans) |
| `match.json` | timeline of every screen seen |
| `trails.json` | players (name, colour, points with time/x/y), rings, scans, items, dropship line, landing, death, downs, POIs - in map-picture pixels |
| `match_paths.svg` | vector version, layers: map (without its icons), rings, scans, items (Care Packages, Evo Harvesters, Replicators, Respawn Beacons, Ring Consoles: one group each, hover for when first seen), dropship, events, players, pois |
| `match_map.jpg` | the final picture |
| `summary.jpg` | the stats screen, to double-check numbers |
| `*_post_game_*.jpg` | only if the stats screen was seen but couldn't be read: its pages are kept at clean-up, to find out why |
| `sheet.json` | which workbook row it went into |
| `fixes.json` | *(optional, yours)* corrections, e.g. `{"mode": "Wildcard", "poi": "The Core", "contested": "Yes", "placement": 4, "legends": {"FabshdC": "Bloodhound"}}` (legends by name as on the stats screen; read fresh every time, so it works after clean-up too) |

A cleaned match (`.cleaned`) is never read again (its stats.json is final). Its picture + SVG can be redrawn:
`.venv/bin/python -c "import Match_Map; Match_Map.redraw_from_trails('Captures/<match>')"`.
Set `KEEP_SCREENSHOTS = True` in `Extract_Stats.py` to keep everything.

### The match picture

Your trail thick, teammates on top and nudged a few px so shared paths show side by side; teammate you
spectated while dead = bold. Dashed thin line = skydive (before landing), or moving faster than on foot (`FLY_SPEED`
in `Match_Map.py`, over at least `FLY_MIN`, 1.5% of the map: a jump tower, balloon, Loba's teleport, a long zipline).
Dotted line = lost and picked up again (not seen for over `TRAIL_GAP`, 8 s, or a jump too far to be the same arrow:
dead, respawned elsewhere, off the minimap); the way they went in between isn't known. Dashed blue line with chevrons =
the dropship's whole flight, circle "jumped hh:mm" = where you left it. Green circle = landed, red X = died
(drawn under the paths), "down/back" = died and came back (respawn). White dots 1m, 2m... = minutes since
the jump on your trail; small X = where a teammate was last seen. Rings: white = ring ("Ring 3 hh:mm"),
grey = next ring. Survey beacon scans: dotted teal circle + pin.
POI names from `Maps/<Map>/pois.json`. Zoomed version frames where the squad went after landing. The map
underneath has no icons (respawn beacons, Replicators, Evo Harvesters, Care Packages, Ring Consoles...) or
printed POI names: see Map pictures below; the items are in the SVG instead.

### All games together (`Heatmap.py`, refreshed after every match)

Each is a transparent layer (`Heatmap.Layer`: no map, POI names or titles on it) at the match pictures' size, for the
Maps tab to stack over the map; what each shows is written under the tab's buttons.


* `Maps/<Map>/layer_heatmap.png` - how many player-trips passed each spot (blue sometimes → red most often; a
  player standing still counts once, so routes / choke points light up), landings as green dots. Only on
  foot: after landing, and any move faster than `FLY_SPEED` (`Match_Map.py`, 0.006 of the map's width a second; skydiving
  ~0.009, on foot 95% under 0.0046) is left out - skydives, jump towers, balloons, respawn drops.
* `Maps/<Map>/layer_land.png` (`Ring_Predict.py --land`) - where players can go and where the last ring can form: white
  = every spot your squads walked (on foot), green = playable (busy places: right next to a path), dim green = quiet
  (counted playable), yellow line = where the last ring's centre can be (70%+ of it on playable ground; its size drawn
  bottom right), blue = gaps the ring-safe route may also cross.
* `Maps/<Map>/layer_rings.png` + `end_zones.json` - where endgames end up: only the last 3 ring sizes; a bigger
  ring is hidden once 3+ games had a smaller ring inside it; per ring size (stage), the rings nearest a POI
  are hosts and any other game's ring overlapping a host by ≥70% joins the one it overlaps most (each ring in one group only; hosts never
  merge with each other, so a blob stays about one ring wide). Drawn as the union of the host and its rings,
  number = games in it (`MERGE_AT` in Heatmap.py). One colour per round (`ROUND_COLOURS`: blue 2, yellow 3, red 4,
  pink 5) with a key. Each ring is drawn at its round's set size (`MapLocator.ring_size`: measured rings wobble; round 5's
  measures small, 82-103% of its size, so down to `LAST_ROUND_LOW`, 75%, it's round 5), and one that's no round's size -
  caught while closing, like the final ring closing to nothing (59% and under) - is left out; a one-game ring is just a thin outline, only groups of 2+ are filled and numbered. Every outline sits on a dark
edge (`RING_HALO`) so it shows over the heatmap's blues and the land's greens / yellow line.

---

## How each stat is worked out

**Screens** (`Screen_References/screens.json`): each screen is recognised by small grayscale template
crops ("anchors") at fixed spots, checked in order, first match wins: waiting (lobby: the WAITING FOR PLAYERS banner, or Ranked's SQUAD RANK DISTRIBUTION title -
`"match": "any"`; Ranked's lobby has no skill graph, but its top-left RANKED / WORLD'S EDGE is the mode / map
fallback when legend select is missed, the map lower down than on the other screens, ~y 204). **Ranked - Dropzone** (squads spawn on the ground, no flight):
read as Ranked with `dropzone` in stats.json; legend select's "WORLD'S EDGE - DROPPING AT: BIG MAUDE" is the landing
POI (matched to the map's POI names); no Dropship Heading / Flight Path and no flight line on the match picture (the live map can't tell yet and may
draw one mid-match; the dropship screen is just the spawn hold,
the compass is wherever you face); the entry cost sits ~65 px lower under the rank badge there), post_game (PASSES tab,
searched along the whole tab row because winners have no DEATH RECAP / LIVE tabs), map (SET WAYPOINT + ABOUT
hints), spectate (SPECTATE tab; the HUD moves down 64 px, so crops use `crop_boxes`), legend_select (Mute
Squad + class names), your_squad, dropship ("IN DROPSHIP" banner - not always on screen), in_game (SQUADS
LEFT). **Wildcard** (10 squads, instant respawns while the squad lives, rings from round 1): its lobby is
recognised by HOW TO PLAY (no skill graph - the skill graph is only read for Trios / Duos), legend select's
anchors are searched ~60 px lower (Cancel Selection pushes them down), Your Squad by its title at the bottom;
legend select has no name bar, so the squad comes from the names top left and your legend from the top right
(teammates' from the squad panel portraits). The dropship again mid-match is a redeploy, not a new match; your
path follows a respawn once 3 placements in a row agree (drawn with a break, not a line across the map).
`RESPAWN_MODES` matches stay out of each map's heatmaps, rings picture, endgame guess and learned ring sizes
(`trails.json` has the mode). **Mixtape** (Control, Gun Run, Team Deathmatch) and **Bot Royale** (Trios against bots, rings from round 1; read
from the lobby / legend select corner, so no skill graph) - `UNTRACKED_MODES` - are never tracked:
no workbook or CSV rows, no map positions, nothing learned about the maps (`Extract_Stats.tracked`); with no mode
read, a match only counts if the battle royale HUD (SQUADS LEFT, what in_game is recognised by) was seen - Mixtape
has none. The log says "not battle royale: not tracked". The map opened while dead has no hints: recognised by lining the middle of the screen up with the
map picture. A match starts at a pre-game screen; going back a stage or a 10 min gap starts a new one. The stats
screen, spectating and the map never start one by themselves (`NOT_A_START`): with no match in the last 10 min they're
ignored (no lobby, no battle royale HUD - 2026-11-03 17:19 the Legend Locker's Banners passed for the stats screen,
17:34 a Firing Range death for spectating, and each got a match folder of its own). In-game (SQUADS LEFT) still does:
the capture joined part-way.
Re-make an anchor after a UI change: `.venv/bin/python Build_References.py <screen> screenshot.png`.

**Stats screen**: every "Kills / Assists / Knocks" label is a card (2 or 3). Each value is read close up
(cleaned black-on-white crop); if that doesn't look like a value, the whole-screen read is used; if the two
differ only by 7↔1 the 1 wins (this font's 1 reads as 7 up close). Placement is re-read close up next to
SQUAD PLACED if garbled; a summary with just MATCH SUMMARY over the cards (no placement on it) counts too. Only the
SUMMARY and EXPERIENCE tabs are kept (`KEEP_SUMMARY_TABS` in Stat_Capture.py), not DEATH RECAP / PASSES, nor lobby
menus mistaken for the summary (the stats screen's PASSES anchor is in the lobby's top bar too: store, locker and
battle pass menus pass for it). Checked two independent ways, once a second on the full-size frame, and that very
frame is what's saved: SUMMARY by its player cards (both the Damage Dealt and Survival Time labels,
`Screen_References/summary__*.png`, found across the card row: real cards 1.0 - green MATCH SUMMARY and red SQUAD
ELIMINATED alike - lobby menus at most 0.46; `CARD_LABEL_MATCH` 0.75) *or* the selected tab's pink-red bar with
SUMMARY read above it; EXPERIENCE by the bar and its name. Tab names are read afresh on every visit to the stats
screen (kept for the whole run, a lobby menu's misread once stuck to SUMMARY's bar position: 2026-11-01 14:14's
summary, a minute on screen, was never saved). No SUMMARY kept yet and nothing made out: a frame every 3 s from 4 s
in, 5 at most (`UNVERIFIED_*`), so a summary the checks miss is still read. `post_game_last.jpg` is the last
checked frame; an unchecked one (a lobby menu) never replaces it (14:14's was overwritten by the Legend Locker),
and a re-read that finds no summary keeps the one an earlier read found (its pages are gone by then). Each kept tab is saved the first frame it's
seen (a SUMMARY tab flicked past on the way to EXPERIENCE is gone a moment later), then SUMMARY once more when it
has been up `SUMMARY_SETTLE` (3) s and holds still (placement / squad kills animate in; its card art never stops
moving - 22:55 saved it 8 times), EXPERIENCE whenever it has settled and filled in more by `SUMMARY_CHANGE` (3;
MATCH PLACEMENT comes last - 6.2, too small for the other screens' 12); `TAB_SAVES` (2 / 4) at most per match. Once
a summary has read fully (every card's numbers, the placement, you on it) the other stats-screen pages are
deleted straight away: only the one read and the fullest EXPERIENCE page stay (a summary that didn't read fully
keeps them all). A card name the whole-screen OCR garbles ("colddudde98" -> "6appnppioo") is
read again close up; whichever legend select backs is kept. No clean-up / read starts while the stats screen is up,
and a match whose match.json changed after its read began (`read_at` in stats.json) is read again (2026-10-31 19:43
was read mid-stats-screen and lost its placement). Of 269 matches to 2026-10-31, 254 had their stats screen read;
of the 15 that didn't, 3 never got past the lobby / legend select / squad intro, 4 left mid-match before any stats
screen (requeued), 4 were stats screens split off by restarts (fixed since), 2 only ever showed EXPERIENCE (now:
its placement), 1 was MATCH SUMMARY and read too early (fixed; re-read), 1 unknown (pages gone). The lobby's `waiting_first.jpg` is taken 3 s in (`FIRST_SHOT_DELAY`), once it has loaded.
Screens are scaled to 1080p first (tested on a 4K Champion screen). EXPERIENCE tab (ranked): placement, entry,
kills/assists RP. No readable summary: placement = the EXPERIENCE tab's (ranked), else last SQUADS LEFT seen; alive at the end with ≤2
squads left = #1 (win). Name matching tolerates cut-off names ("Spookyrabi") and club tags.

**Legend select**: legends + names along the bottom; the last pick is often revealed after the screen
stops being recognised, so the frames in between are read too. Season 31 moved the class labels and Mute Squad
(anchors recut 2026-11-03 from a Trios pick, 19:03; the same in Ranked: 0.79-1.0 on S31's legend selects, every other
screen ≤ 0.28). Should a new layout stop it being recognised again: once the lobby is left, unrecognised frames
(`pick_reveal`, every 3 s, 40 at most) are kept until the squad intro and read the same way (S31's first games were read
like that) - and they're the screenshots to recut the anchors from (`Build_References.py legend_select <frame>`). Teammates = everyone who picked; "Empty" only
for a slot with nobody (without legend select, the stats screen's player count says so). **Fallback**
(`Legend_Icons.py`): when legend select is missing or a pick wasn't read, each player's legend comes from the
portrait left of their name on the squad panel (in-game crops, while alive): the Legend Locker's portraits
(`Screen_References/legend_icons/<Legend>.png`) matched in grayscale on the face (the panel tints them in the
player's colour and cuts them slanted), at most 12 shots, a shot counts at match strength ≥ 0.7 and 0.15 ahead of
the next legend, 2+ agreeing shots (2/3 of them) decide; kept in stats.json `panel_legends`. Typed-in legends
(Matches > Set legends) beat legend select, which beats the portraits. Old cleaned matches have no squad crops
left, so for them it's still Set legends.

**Dropship heading**: the compass number between the chevrons. First reading that stays the same for 3 s in
the first seconds of the match (you on the ship looking forward); else the dropship screen; else the first
in-match frame (only if the match was seen from the start, and checked against the dropship's movement).

**Dropship line** (`Match_Map.best_flight`): through where the dropship minimaps put you (else where you jumped)
along that heading (else the way the dropship minimaps moved). If you open the full map in the dropship or up to
90 s after the jump (`FLIGHT_SHOT_AFTER`), the game's orange flight line on it is the real path: when it runs within
15° of the heading (`FLIGHT_AGREE`) it sets where the line runs and its exact angle (`"from_map"` in trails.json).
With no heading at all it's still drawn, without chevrons. Checked on 52 dropship map shots: the heading + minimaps
line was within 1° and a few px of the real one, except one match with no compass and no dropship minimaps (60° off).
The live map does the same; minimaps / map shots seen while it's still working out which map it is are kept and
placed once it knows (before, the dropship part was lost whenever the map changed from the last game).

**Entry cost**: S31's Ranked lobby shows `ENTRY COST: <n> RP` bottom right once the match type has loaded
(`parse_lobby_entry`, newest lobby shot first; before that it says MATCH TYPE LOADING...) - that wins, it's big and clear;
else the dropship / first frame's "-20 RP" under the rank badge (when both are read and differ, stats.json warns);
missing → nearest ranked game. **Percentile**: positions of
the skill graph's shaded band start, red YOUR TEAM line and band end, 0-100 along the track (raw, no fitting). The band is
the unbroken stretch of flat light grey around the red line (`GRAPH_BREAK`: breaks up to 6 px bridged): the scene behind the
clear part of the track can be as light in places (2026-11-03 01:29: a white building read as the band starting at 10%, not
57%). Older games with a very wide band (start under ~12% or 60+ points wide) may have the same misread; their lobby shots are gone.
**Skill**: (2×start + team + 4×end) / 7. **EST XP**: sheet formula. **Most Damage?**: your damage / squad's.

**Player ranks** (Ranked, S31 on: `parse_rank_badges` -> Squad Stats' Rank): every squad member's rank badge left of their
name, in the lobby's squad list and legend select's (pick order; both read, every lobby shot and legend select frame).
Tier from the emblem's colour inside the badge (`BADGE_COLOURS`: Bronze 139,93,71 and Silver 148,160,161 measured on the
badges, the rest estimates until seen; more than 15 off every tier = no badge, like Trios' portraits); division from the
plaque's numerals matched against `Screen_References/rank_numerals/` (II / III / IV cut from S31 badges, I made from III's
middle stroke until one's seen; counting strokes misread the lobby's blurrier III as IV); the name next to it by OCR
(club tag dropped). Each player gets the rank most of the readings near their name (`name_similarity` ≥ 0.6) agree on.
Kept as `rank_badges` in stats.json. 2026-11-03: 18:40 Silver IV x3, 19:09 Silver III x3, 19:20 Silver III, Silver III,
Bronze II - all 30+ readings per game agree.

**Rank Distribution** (Ranked only, `parse_rank_distribution`): the lobby's rank distribution bar (x 173-806,
y 946-982 at 1080p; Season 31 moved it there, bottom left - S30's was x 644-1277, y 996-1031, under a SQUAD RANK
DISTRIBUTION title) has one segment per rank, as wide as its share of the squads, the count in it and the rank
icon under it. The bar is cut where its colour changes (rows above the count, which are flat; the lower half is
dotted) and each segment named by the nearest colour in `RANK_COLOURS` (Rookie, Bronze, Silver, Gold, Platinum,
Diamond, Master, Apex Predator - the official names). Counts come from the widths: the whole bar is 20 squads, so a
segment is 31.65 px a squad (11.0 and 8.97 wide on 2026-10-31 20:19, where OCR read the 9 as a 6); OCR of the bar
only when the widths aren't whole squads of a full lobby. A squad not in yet is a dark slot at the end of the bar (~10,17,22): `Empty`, counted in the 20 but left out of the cell (2026-11-03 18:40 read 19 Silver + 1 empty as "Bronze 1, Silver 19"). One cell, lowest rank first: "Gold 4, Platinum 16". Every
lobby shot is read and the most believable kept (`rank_shot_score`: colours near their rank's, whole squads, OCR
agreeing; `rank_distribution_from` in stats.json) - `waiting_last.jpg` can be the fade / wipe into legend select,
which read as "Diamond 11". Platinum and Diamond are measured on the bar (83,232,254 and 12,140,248), Bronze and Silver on S31's
(140,104,92 and 174,174,184); the others are each rank's emblem colour on the Ranked Leagues page (the bar differs
from the emblems: Diamond's is 84,134,247, Bronze's 183,121,90), so a lobby with other ranks keeps the bar + icons as `rank_distribution.jpg` in the match folder (kept
at clean-up) to check them by - the Lobby view (the same best shot) prints each segment's bar colour and how far it
is from its rank's. The lobby keeps filling while it's up (S31's first: Bronze 4 / Silver 16 at 50/60 players, 3 / 17 at
58/60), so between equally believable shots the newest wins. Ranked's lobby is recognised by S31's `SQUAD TIER: <rank>`
label top left (the `squad_tier` anchor: 1.0 there, other screens at most 0.35) or S30's SQUAD RANK DISTRIBUTION
title (the `rank_distribution` anchor) scoring at least `RANK_TITLE_MATCH` (0.85: Ranked's lobbies 0.96-0.99, the
Trios lobby's PLAYERS SKILL DISTRIBUTION in the same spot 0.60-0.64 - over the capture's 0.6 for "a lobby", so
2026-10-31 22:55-23:08's Trios games were read as Ranked until it had its own bar; rows 1057-1059 fixed), also for
the Lobby view mid-match and as the mode when the corner is caught half animated in. Shown on the Matches tab and the
Latest Match tab ("lobby: Platinum 20"). The Ranked games before 2026-10-31 18:49 have none (lobby shots cleaned).

**Map positions** (`Map_Locate.py`): every minimap is matched (SIFT features) against the map picture and
the stitched minimaps; a minimap that can't be placed is lined up with the one before/after it (≤4 s; it may be zoomed up to 35%
more or less - late in the game, and spectating, the red tint hides most of the map and the minimap zooms in steps).
The live map lines them up backwards too: minimaps it couldn't place (up to `UNPLACED_KEEP`, 60) wait until one is placed,
then are lined up back from it, one after another (2026-11-03 01:57: a teammate you watched in a dark spot walked 45 s
with only borderline map matches; lined up forwards only, the live map lost them while the final read had them).
Across the last big zoom-in (~2.5x at once, little left but the ring hole) the hole itself places it: the same ring
a second apart gives the new zoom and position. A direct match further from the last placed minimap than anyone
can move is checked by lining the two up (wins if they line up; a respawn / switching who you spectate doesn't).
Lined-up minimaps keep their zoom (they're past the zoom levels direct matches reach).
Fits must be north-up, at a zoom level the match uses consistently (the minimap zooms in late game /
spectating), and at a believable speed. Map unknown (joined mid-match) → worked out from the minimaps.

**Squad**: colours (cyan / green / orange) - yours from the arrow in the middle of your minimap while
alive, names from the squad panel (a name read twice is dropped). If no panel name matches the end card,
every other colour is followed and only as many trails as you had teammates are kept (the most seen). Teammate arrows: solid blobs of
their colour shaped like an arrow (outline compared with `Screen_References/squad_arrow.png`, any heading) and
arrow-sized (42-90 px on the 1080p minimap; 25+ px if very arrow-like, i.e. partly under another arrow): respawn
beacons (green), Evo Harvesters (yellow), Replicators (blue), lava and the ring's tint (orange) share squad colours
but aren't arrows (checked on a match: map art let through fell from ~1 in 25 green / 1 in 3 orange blobs to
~1 in 500 / 1 in 300). Also not on a same-coloured icon of the map picture, not within 18 px of the minimap's edge
(a teammate off the minimap shows as a pointer 10-16 px inside the edge, which isn't where they are), believable speed, 4 steady sightings
to (re)start a trail, median-smoothed. A teammate who quits drops off the squad panel (names read every 5 s; a
dead teammate stays on it): once their name is missing from 4 reads in a row where yours reads fine, their trail
ends where they were last on the panel (after that their colour on the minimap is map art - Olympus is full of
orange), live and in the final picture; they still count as a teammate. While spectating, the middle arrow is that teammate. While the full
map is open: everyone's arrows read off the map itself, but between two minimap sightings only if it's on the way from
one to the other (10 px + 4 px per second off the line; the nearest orange to you on the map can be lava, which
showed as you jumping ~50 px and back each time you opened the map).

**POI** = nearest POI to where you landed (stayed put 8 s after the jump). **Death Zone** = where the last
surviving squad member went down (the minimap follows who you spectate); per player in Squad Stats = where
their trail ended. **Contested** (best guess, no reliable way to know): within 15 s before - 2 min after
landing the kill feed shows "Squad landed nearby!", an enemy spotted, an enemy shield broken, or a fight
between your squad and another. Reasons kept in stats.json.

**Rings**: full map (only the map area: the game and button bar beside it are cut off) - ring = the
untinted hole inside the red/pink "outside" tint, tinted just outside and clear just inside its edge (also
looked for with thin red map lines filtered out, e.g. Quarantine Zone's wall cutting across the ring); next ring = lighter grey
disc (blue/coloured discs are domes, rejected; so is any next ring found by any method that's half or more a survey
scan's teal inside - the scan, or a pulse spreading out from it, once came out as the next ring on Olympus, where the
sky around the map hid the "more saturated inside" test - or 30% when it's the map's scan size (±10%, `SCAN_SIZED`: over
E-District's purple roofs a scan reads only 45% teal and came out as a 239 px next ring, 2026-11-03 18:27); a small late one is looked for inside the ring, in what's drawn
over the map picture, and counts once seen on 2 shots at different times; any next-ring disc that's a lighter
circle on the map picture too is a map feature, dropped); a ring hole that's bright colour inside is a map icon
on the tint (ammo, replicator), not a ring; a hole's outline has to be 60% on its circle, 45% if it's one of the map's ring sizes (`HOLE_SUPPORT_KNOWN`: E-District's purple roofs pass for the pink tint and break the outline up - 2026-11-03 21:31's round 3 ring read 53%; on the 104 kept in-game map shots this only swapped 13 px icon "rings" for real ones); partly-visible rings fitted with this map's known ring
sizes (`info.json`, learned per game); a ring caught while the timer says it's closing isn't kept. Minimap -
same hole, only with at least 5% of the minimap red tint (`MINIMAP_TINT_MIN`: the hole finder also takes Olympus'
pink-over-sky tint, and E-District's purple roofs pass for it - gaps between them came out as 70-105 px rings at
Stadium with 0.9-2% red, real late-game holes have 7-34%) (whole late-game rings; within 15% of a known size it's refitted at exactly that size, as the
free measurement wobbles ~10%) or an arc of the red tint's edge fitted with known sizes (70% of the edge on the circle;
edge points on map icons don't count - a beacon on the edge dents it; an arc that still lies on the ring already known is that ring, not a new one, and an arc two sizes fit about equally (≥90%) is too short to tell its curve, so it's dropped - a nearly straight bit of the 300 px ring's edge once fitted a 147 px ring bent the other way; while no ring is known yet, circles 1.4x / 1.8x the biggest size are tried too (`MINIMAP_DECOYS`) - before round 1 has closed the edge is the map's starting circle closing in, ~510 px on Worlds Edge, never a learned size, and its gentle arc fitted the 300 px size 230 px from where round 1 really was: if a bigger circle wins or ties, the arc says nothing); a ring must hold the same circle 3 s (3
sightings). Once the next ring is known (map / grey disc), the minimap's red edge lying exactly on it means it has
closed (while closing the edge is bigger and off-centre): 2 sightings, no wait. The minimap only sees the ring when its
edge is on the minimap (near the edge, or late game), so early on you usually see it on the map first.
**Every ring is inside the one before**: the rings found so far (only sure ones: seen on 2 map shots at different times,
or a minimap run - `Map_Locate.ring_prior`) limit the search on the next map shot / minimap to ring sizes no bigger than
the ring and circles inside it, live and in the final read. A ring on a map shot also needs the red tint just outside it
(≥ 40%) and clear map inside (≤ 15% red): real rings 49-100% / 0-7%, the red marker circles and lava fits that came out as
tiny rings 6-16% / up to 69%.
Next ring from the minimap's lighter disc. Also from the minimaps / map while spectating another squad
after yours is out (the ring is the same for everyone; often one more than you saw). Sightings of one ring (up to ~15% apart) are drawn once, at their median. Kept only if consistent: centred on the map, never growing, each
inside every earlier circle at least as big (not just the latest: the first ring seen again after the next ring once let a smaller circle outside the next ring through; it may stick out by `RING_INSIDE_SLACK`, 15% of its own radius + 3 px, for measuring wobble - a share of the bigger ring let a 27.5 px round 5 ring sit 9 px out of round 4 on 2026-11-03); a next ring smaller than every ring size only once the ring is the smallest size (a 17 px "next ring" during round 4 was a lighter patch inside it); rings seen at two different times are checked first, and a ring seen once (a red
marker circle, a bad fit) only counts if no confirmed ring later on is bigger (once, a 63 px fit on a marker taken
first hid every real ring after it). **Map shots beat minimap arcs**: a minimap ring the size of a ring seen on two map shots (±15%) but somewhere else is dropped, whenever it was seen (2026-11-03 00:33's wrong minimap ring came first and kept the map's round 1 ring out); so is a minimap ring a next ring seen on two map shots sticks out of (next rings only: a small map-shot "ring" can be an icon on the tint) - 2026-11-03 19:46's round 3 fit ~60 px off kept the round 4 next ring out. **One round, one place**: a circle the size of one already kept (±15%) has to be
where that one is, so a later minimap fit of the round 2 size somewhere else isn't a second round 2 ring (2026-10-28,
a 148 px minimap ring 270 px north of the round 2 next ring). A next ring on a map shot (any way it was found) that's
a lighter circle on the map picture too (`DISC_ON_MAP_MAX`) is part of the map: Thermal Station's round building fitted
with the 60 m size reads 97% lighter inside there, real rings 11-29%. Checked against the map's ring sizes (±12% or 4 px is the same size; **ring sizes**: rounds 2-5 are the same
in every map in game metres - radius 320 / 200 / 100 / 60 m, `RING_RADII_M` (the wiki says 325 / 200 / 100 / 50; your rings measure 319-322 /
199-200 / 99-100 / ~57) - and round 1 differs per map, so
round 1 is learned from your games and the rest is metres x the map's px per metre, `px_per_m` in `info.json`, worked out from
the learned sizes that fit those rounds: ~0.46 on Worlds Edge / Olympus, 0.63 on Broken Moon; until a map has it, the learned
sizes as they are. `PX_PER_M_FIXED` (Map_Locate.py) locks a map's scale in by hand over what's learned, and `RING_SIZES_FIXED` its
rounds 2-5 sizes where they aren't the usual metres: E-District 0.975 px/m, rings 315 / 155 / 96.5 / 48 px (323 / 159 / 99 /
49 m - its round 3 is ~159 m, not 200; whole rings on 2026-11-03 20:40's map shots, round 5 from 18:40); Storm Point
(reworked) 0.967 px/m, rings 309.5 / 147.5 px measured (2026-11-03 23:16 + 23:37) and 92 / 46 px from E-District's ratios
until rounds 4-5 are seen - the scale it learned from rounds 2-3 (1.509) took the 309 px ring for round 3 and drew it at
301.8 px. E-District has no round 1 size: its
first ring takes in the whole map (no tint on the dropship's map), so anything bigger than round 2 is a ring closing in -
not kept, like a ring caught mid-close anywhere. Round numbers come from the size (`ring_round`: 312 px is round 2, 320 m,
with or without a round 1 size); the endgame guess counts the map's sizes and shows them shifted by the missing round 1. A size caught mid-close can't become a "size" that way; the last ring closes to nearly nothing, so a
ring smaller than round 5 is fine. The SVG gives each ring its round and radius in metres, e.g. "round 3, 200 m") (live and final, and for the
end zones): a ring bigger than the first ring (largest size) or between two sizes one stage apart (ratio ≥ 0.4)
was caught mid-close and isn't kept (where it's going is the next ring), nor is an off-size next ring; sizes in a
wider gap (the last, rarely-seen rings) are kept. The sizes only kick in once a map has learned 2+ (or has a locked scale: before E-District's, its minimaps' lighter discs -
lit round plazas, 51-118 px - weren't size-checked and came out as small next rings, 2026-11-03 17:34's 81 / 52 px). **Survey scans**: dark teal discs on the full map (not teal on the map picture),
always the same size on a map (`SCAN_SIZES` in `Match_Map.py`, share of the map width, ±8%; Worlds Edge 0.222, Olympus 0.222,
E-District 0.232 (233-242 px, 2026-11-03);
a map without a size gets no scans - teal water would be guessed - so add one from a screenshot of a scan),
counted once each.

**Map pictures** (`Maps/<Map>/`): `reference_base.png` is the original (a zoomed-out full-map screenshot from
the dropship). Every full-map shot is lined up and kept in `screens/` as lossless WebP (newest 75 dropship shots
+ 25 others, `CLEAN_SCREENS` / `CLEAN_OTHER`; ~1-2 MB each), its alpha saying per pixel: map / overlay (ring, tint,
cursor, dropship line - worked out once, when kept) / not on the shot. `reference.png` = per-pixel median of the
dropship shots with the overlays left out (the in-match shots vote too while there are under 10 dropship ones - but not one that's over 25% red tint, `TINTED_SHOT`: the tint over E-District's purple roofs is pink, which the tint mask misses, and 23 late-game shots turned its roofs pink on 2026-11-03), so
it gets cleaner the more you open the map in the dropship. The shots are stacked on disk (`.cleaning/`, deleted
after) and the median taken 64 rows at a time: ~0.6 GB with 75 shots on Broken Moon (all at once took 4.7 GB with
25). Respawn-mode matches (Wildcard: a darker map with rings from the start) add nothing to a map: no shots, stitched
minimaps or ring sizes. `stitched.png` = your minimaps stitched at
their sharper detail (scans / ring tint excluded), laid over the reference when drawing. Positions always
stay in reference_base pixels. **Without icons** (`Map_Icons.py`, drawing only - the originals keep their icons,
positions are found on them): `reference_clean.png` = reference.png with its icons (big diamonds, templates in
`Screen_References/map_icons/far/`), little coloured dots and printed POI names painted over; `stitched_icons.png` =
where stitched.png has icons (templates `Screen_References/map_icons/<kind>.png`, cut from a 1080p in-game map,
matched at 0.9-1.3x): the clean reference shows there instead. Both are remade when their source changes (after a
match); `.venv/bin/python Map_Icons.py ["Olympus"]` remakes them now. A few faint, half-blended icons stay.

**Items** (`Map_Icons.match_items`): the same templates on every 3rd minimap (0.7-0.85x) and every full-map shot
(0.55-1.1x), match ≥ 0.72; sightings of a kind within 8 px are one item, kept if seen twice; first-seen time kept
(Care Packages land during the match).

## Accuracy (checked against 52 hand-recorded games, 26 Oct 2026)

Entry 52/52 · Game Mode 51 · Placement, kills, assists, knocks, time, revives, respawns, squad kills 50
(the 2 misses were wins before win screens were read) · Damage 49 (the miss: the card shows what was
recorded) · Map 50 (misses were hand typos) · POI 49 · Death Zone 46 (mostly two POIs equally close) ·
Dropship Heading 46 (early games without the compass recording) · Teammates 43 · Contested 39 (inherently
fuzzy). Capture can't see games played while it's off.

## Maintenance

* **Map updated / new map**: open the full map zoomed out in the dropship, then
  `.venv/bin/python Map_Locate.py --reference-from Captures/<match>/<..._map_01.jpg> --map "<Map>"`
  (makes a new reference_base + POIs from the printed labels; check/edit `pois.json`: OCR misses labels an icon
  sits on). Worlds Edge, Olympus, Broken Moon, E-District (2026-11-03, S31's first E-District game; "Posort" /
  "Stadoom" fixed, Blossom Drive and Viaduct added by hand) and Storm Point (2026-11-03 22:58, the reworked map:
  "Devestated" / "Frho Ho" / "Launsli Pao" fixed, North Pad, The Wall, Zeus Station and The Pylon added by hand)
  are current; Kings Canyon still has an old spreadsheet image. Wildcard maps need this once.
* **Screen stops being recognised** (UI update): `Build_References.py` with a fresh screenshot.
* **New legend** (or the locker's portraits change): screenshot Main Menu > Legend Locker > Legends at 1080p, add
  the legend to `LEGENDS` in `Extract_Stats.py`, update `LOCKER_ROWS` (left edge of each portrait, 83 px apart) and
  `LOCKER_LEGENDS` (same order) in `Legend_Icons.py`, then `.venv/bin/python Legend_Icons.py --from-locker
  "<screenshot>"`. Check a match with `.venv/bin/python Legend_Icons.py Captures/<match>` (needs its screenshots).
* **Re-read a match that still has its screenshots**: `.venv/bin/python Extract_Stats.py Captures/<match>`
  (`--force` = all, `--no-sheet` = don't touch the workbook, `--clean` = delete screenshots of read matches
  now, without the 20 minute wait).
* **Old recording**: `.venv/bin/python Stat_Capture.py --video file.mp4` (stays out of the workbook).
* **Heatmaps by hand**: `.venv/bin/python Heatmap.py ["Olympus"]`.

## Files

| | |
|---|---|
| `Stat_Capture.py` | the background capture (OBS WebSocket, screen tracking, what to save, live map, starts the reading) |
| `Screen_Detection.py`, `Screen_References/` | screen recognition; `screens.json` = anchors, crop boxes, save rules |
| `Build_References.py` | cut new anchor crops from a screenshot |
| `Extract_Stats.py` | reads a match into stats.json / CSV rows; clean-up; settings at the top |
| `Legend_Icons.py`, `Screen_References/legend_icons/` | legends from the squad panel portraits (fallback for legend select) |
| `Map_Icons.py`, `Screen_References/map_icons/` | map icons: per-match items (SVG layer) and the icon-free drawing background |
| `Map_Locate.py` | minimap → map positions, POI / Death Zone / Contested, rings, map pictures |
| `Match_Map.py` | drawing (match pictures, SVG), live map, squad colours, scans, rings on the full map |
| `Heatmap.py` | all-games heatmaps and ring end zones |
| `Sheet_Write.py`, `Calc_Macro.py` | workbook writing (the macro runs inside headless LibreOffice) |
| `Seasons.py` | season start times, which season a match is in, each season's workbook |
| `Past_Seasons.py`, `Past_Seasons/` | your past seasons' Google Sheets (.xlsx) -> `past_games.csv` for the panel |
| `restart_capture.sh` | (re)start the service |
| `Control_Panel.py` | the window: stats (overview, matches, maps) and the capture controls |
| `Maps/<Map>/pois.json`, `reference_base.png` | tracked; everything else in Maps/ is rebuilt as you play |

Git tracks code and fixed files only; the workbook, Captures/, pictures, heatmaps, rings, end zones,
stitched maps and `info.json` are rebuilt and ignored. The work lives on the `stat-capture` branch.

Older files from before this setup (not used): `Extract_Screenshots.py` (a template-creator GUI),
`TemplateCreator.py`, `Image_Sorting.py`, `Main.py`, `Frame_Templates/`. The original design notes are in the
first commit: `git show 1fbb578:Readme.md`.

## Ideas not built yet

* Ring prediction: the next ring must sit fully inside the current one and on playable ground; with more
  end zones per map, shade where the next ring can still go on the live map.
* Per-POI landing stats (placement, damage, contested rate), death heatmap, ring timing vs placement,
  legend / comp performance from Squad Stats, dropship path vs result, scans vs placement.
