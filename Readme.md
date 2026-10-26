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
   ├─ Sheet_Write.py + Calc_Macro.py ─► "Apex Game Data S30.ods" (Past Games + Squad Stats)
   ├─ clean_up() ─ deletes the screenshots it no longer needs
   └─ Heatmap.py ─► Maps/<Map>/heatmap.png, heatmap_me.png, rings.png
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

The capture runs as a systemd **user** service (a transient unit; it does not start by itself after a
reboot / log-in, run the restart script once):

| Command | What it does |
|---|---|
| `./restart_capture.sh` | start / restart the capture now |
| `./restart_capture.sh --after-match` | restart once the current match is in the workbook (use after code changes so a live match isn't cut) |
| `systemctl --user status apex-stat-capture` | is it running? |
| `tail -f Captures/capture.log` | what it's seeing (screens, matches, reading, workbook writes) |
| `Captures/libreoffice.log` | workbook jobs |

The restart script runs `systemctl --user reset-failed` first: a service that crashed while stopping
stays "failed" and refuses to start again (this once stopped capture for an hour).
Run the `--after-match` variant itself under systemd so it survives:
`systemd-run --user --unit=apex-restart-later --working-directory="$PWD" "$PWD/restart_capture.sh" --after-match`

**OBS sources:** Image sources pointing at `Latest Match Map.png` and `Latest Match Map Zoomed.png` in this
folder (OBS reloads them when they change). The file always shows the newest match: live during a match,
then replaced by the final version 1-3 minutes after the stats screen.

---

## What you get

### The workbook (`Apex Game Data S30.ods`)

Written by headless LibreOffice after every match. **Never while the file is open in Calc**: the match
waits (`Captures/.sheet_pending`) and goes in within a minute of closing it. Matches you typed in by hand
are recognised (same placement + damage within 10 min) and skipped. Tabs: Past Games, Squad Stats,
Ranked RP Info (the RP formulas use it), Ring Info, Legend Classes.

**Past Games** - one row per match (next row with no Placement). Written: A Placement, B Squad Kills,
C Kills, D Assists, E Knocks, F Time, G Revives, H Respawns, I Damage, J Legend, K Time (end), L Most Damage?
(your share of squad damage, %), O Game Mode, P Map, Q Friends, R/S teammate legends ("Empty" = nobody in that
slot), T Entry (cleared when not ranked), Y Dropship Heading, AA Percentile. **Only filled if the cell is
empty** (so your typing wins): M Death Zone, U POI, V Contested. Formula columns (Z EST XP, AB Skill, AC Squad,
AD-AH RP) are left to the workbook (filled down to row 5000).

**Squad Stats** - one row per player per match (filter Is Me = Yes for your own games): Match, Time, Game
Mode, Map, Placement, Squad Kills, Entry, Percentile, Dropship Heading, Flight Path, POI, Death Zone (each
player's own), Contested, Survey Scans, Stats Screen (Yes / Missed), Slot, Player, Is Me, Legend, Teammates,
Squad, Kills, Assists, Knocks, Damage, Damage %, Survival Time, Revives, Respawns.

`Captures/Sheet Rows.csv` and `Captures/Squad Stats.csv` hold the same rows (rebuilt every time).

| Command (`.venv/bin/python Sheet_Write.py ...`) | |
|---|---|
| *(no options)* | add matches not in the workbook yet |
| `--refresh` | rewrite rows the scripts wrote with the latest readings (never blanks a cell, never touches hand-typed rows) |
| `--rebuild-squad` | start Squad Stats over from every captured match |
| `--fill-in` | fill empty Death Zone / POI / Contested |
| `--remove <match folder name>` | take a match back out |
| `--unhide` | show hidden tabs |

All of these refuse to run while Calc has the file open. Back the workbook up yourself (it's not in git).

### Per match: `Captures/<YYYY-MM-DD_HH-MM-SS>/`

While a match is running/being read every screenshot is kept (minimap every 1 s, kill feed every 2 s for
4 min after the jump, SQUADS LEFT every 5 s, compass every 1 s for the first 15 s, a full frame every 45 s,
every map open + every 2 s while it stays open, all end-of-match tabs, plus unrecognised screens right
after legend select and after the match). Once read and written, `clean_up()` keeps only:

| File | |
|---|---|
| `stats.json` | everything read (summary cards, legend select, dropship, location: landing, death, squad_out, players_out, contested + evidence, rings, scans) |
| `match.json` | timeline of every screen seen |
| `trails.json` | players (name, colour, points with time/x/y), rings, scans, dropship line, landing, death, downs, POIs - in map-picture pixels |
| `match_paths.svg` | vector version, layers: map, rings, scans, dropship, events, players, pois |
| `match_map.jpg` | the final picture |
| `summary.jpg` | the stats screen, to double-check numbers |
| `sheet.json` | which workbook row it went into |
| `fixes.json` | *(optional, yours)* corrections, e.g. `{"poi": "The Core", "contested": "Yes", "placement": 4}` |

A cleaned match (`.cleaned`) is never read again (its stats.json is final). Its picture + SVG can be redrawn:
`.venv/bin/python -c "import Match_Map; Match_Map.redraw_from_trails('Captures/<match>')"`.
Set `KEEP_SCREENSHOTS = True` in `Extract_Stats.py` to keep everything.

### The match picture

Your trail thick, teammates on top and nudged a few px so shared paths show side by side; teammate you
spectated while dead = bold. Dashed thin line = skydive (before landing). Dashed blue line with chevrons =
the dropship's whole flight, circle "jumped hh:mm" = where you left it. Green circle = landed, red X = died
(drawn under the paths), "down/back" = died and came back (respawn). White dots 1m, 2m... = minutes since
the jump on your trail; small X = where a teammate was last seen. Rings: white = ring ("Ring 3 hh:mm"),
grey = next ring. Survey beacon scans: faint dotted violet circle + pin.
POI names from `Maps/<Map>/pois.json`. Zoomed version frames where the squad went after landing.

### All games together (`Heatmap.py`, refreshed after every match)

* `Maps/<Map>/heatmap.png` - how many player-trips passed each spot (blue sometimes → red most often; a
  player standing still counts once, so routes / choke points light up), landings as green dots.
* `Maps/<Map>/heatmap_me.png` - your own trails only.
* `Maps/<Map>/rings.png` + `end_zones.json` - where endgames end up: only the last 3 ring sizes; a bigger
  ring is hidden once 3+ games had a smaller ring inside it; rings of different games overlapping ≥90% are one
  circle with the number of games inside.

---

## How each stat is worked out

**Screens** (`Screen_References/screens.json`): each screen is recognised by small grayscale template
crops ("anchors") at fixed spots, checked in order, first match wins: waiting (lobby), post_game (PASSES tab,
searched along the whole tab row because winners have no DEATH RECAP / LIVE tabs), map (SET WAYPOINT + ABOUT
hints), spectate (SPECTATE tab; the HUD moves down 64 px, so crops use `crop_boxes`), legend_select (Mute
Squad + class names), your_squad, dropship ("IN DROPSHIP" banner - not always on screen), in_game (SQUADS
LEFT). The map opened while dead has no hints: recognised by lining the middle of the screen up with the
map picture. A match starts at a pre-game screen; going back a stage or a 10 min gap starts a new one.
Re-make an anchor after a UI change: `.venv/bin/python Build_References.py <screen> screenshot.png`.

**Stats screen**: every "Kills / Assists / Knocks" label is a card (2 or 3). Each value is read close up
(cleaned black-on-white crop); if that doesn't look like a value, the whole-screen read is used; if the two
differ only by 7↔1 the 1 wins (this font's 1 reads as 7 up close). Placement is re-read close up next to
SQUAD PLACED if garbled. A late extra shot is taken 3 s after the summary appears (numbers animate in).
Screens are scaled to 1080p first (tested on a 4K Champion screen). EXPERIENCE tab (ranked): placement, entry,
kills/assists RP. No stats screen (requeued): placement = last SQUADS LEFT seen; alive at the end with ≤2
squads left = #1 (win). Name matching tolerates cut-off names ("Spookyrabi") and club tags.

**Legend select**: legends + names along the bottom; the last pick is often revealed after the screen
stops being recognised, so the frames in between are read too. Teammates = everyone who picked; "Empty" only
for a slot with nobody.

**Dropship heading**: the compass number between the chevrons. First reading that stays the same for 3 s in
the first seconds of the match (you on the ship looking forward); else the dropship screen; else the first
in-match frame (only if the match was seen from the start, and checked against the dropship's movement).

**Entry cost**: from the dropship / first frame; missing → nearest ranked game. **Percentile**: positions of
the skill graph's shaded band start, red YOUR TEAM line and band end, 0-100 along the track (raw, no fitting).
**Skill**: (2×start + team + 4×end) / 7. **EST XP**: sheet formula. **Most Damage?**: your damage / squad's.

**Map positions** (`Map_Locate.py`): every minimap is matched (SIFT features) against the map picture and
the stitched minimaps; a minimap that can't be placed is lined up with the one before/after it (≤4 s).
Fits must be north-up, at a zoom level the match uses consistently (the minimap zooms in late game /
spectating), and at a believable speed. Map unknown (joined mid-match) → worked out from the minimaps.

**Squad**: colours (cyan / green / orange) - yours from the arrow in the middle of your minimap while
alive, names from the squad panel (a name read twice is dropped). If no panel name matches the end card,
every other colour is followed and only as many trails as you had teammates are kept (the most seen). Teammate arrows: small solid blobs of
their colour, not on a same-coloured map icon (respawn beacons, lava), not within 18 px of the minimap's edge
(a teammate off the minimap shows as a pointer 10-16 px inside the edge, which isn't where they are), believable speed, 4 steady sightings
to (re)start a trail, median-smoothed. While spectating, the middle arrow is that teammate. While the full
map is open: everyone's arrows read off the map itself.

**POI** = nearest POI to where you landed (stayed put 8 s after the jump). **Death Zone** = where the last
surviving squad member went down (the minimap follows who you spectate); per player in Squad Stats = where
their trail ended. **Contested** (best guess, no reliable way to know): within 15 s before - 2 min after
landing the kill feed shows "Squad landed nearby!", an enemy spotted, an enemy shield broken, or a fight
between your squad and another. Reasons kept in stats.json.

**Rings**: full map (only the map area: the game and button bar beside it are cut off) - ring = the
untinted hole inside the red/pink "outside" tint, tinted just outside and clear just inside its edge (also
looked for with thin red map lines filtered out, e.g. Quarantine Zone's wall cutting across the ring); next ring = lighter grey
disc (blue/coloured discs are domes, rejected); partly-visible rings fitted with this map's known ring
sizes (`info.json`, learned per game); a ring caught while the timer says it's closing isn't kept. Minimap -
same hole (whole late-game rings) or an arc fitted with known sizes; a ring must hold the same circle 25 s.
Next ring from the minimap's lighter disc. Kept only if consistent: centred on the map, never growing, each
inside the previous circle. Checked against the map's learned ring sizes (live and final, and for the
end zones): a ring bigger than the first ring (largest size) or between two sizes one stage apart (ratio ≥ 0.4)
was caught mid-close and isn't kept (where it's going is the next ring), nor is an off-size next ring; sizes in a
wider gap (the last, rarely-seen rings) are kept. The sizes only kick in once a map has learned 2+. **Survey scans**: violet discs on the full map (not violet on the map picture),
radius 7-17% of the map width, counted once each.

**Map pictures** (`Maps/<Map>/`): `reference_base.png` is the original (a zoomed-out full-map screenshot from
the dropship). Every full-map shot is lined up and kept in `screens/` (newest 25 dropship shots + 10 others);
`reference.png` = per-pixel median of the dropship shots with rings / tint / cursor / dropship line masked
out, so it gets cleaner the more you open the map in the dropship. `stitched.png` = your minimaps stitched at
their sharper detail (scans / ring tint excluded), laid over the reference when drawing. Positions always
stay in reference_base pixels.

## Accuracy (checked against 52 hand-recorded games, 26 Oct 2026)

Entry 52/52 · Game Mode 51 · Placement, kills, assists, knocks, time, revives, respawns, squad kills 50
(the 2 misses were wins before win screens were read) · Damage 49 (the miss: the card shows what was
recorded) · Map 50 (misses were hand typos) · POI 49 · Death Zone 46 (mostly two POIs equally close) ·
Dropship Heading 46 (early games without the compass recording) · Teammates 43 · Contested 39 (inherently
fuzzy). Capture can't see games played while it's off.

## Maintenance

* **Map updated / new map**: open the full map zoomed out in the dropship, then
  `.venv/bin/python Map_Locate.py --reference-from Captures/<match>/<..._map_01.jpg> --map "<Map>"`
  (makes a new reference_base + POIs from the printed labels; check/edit `pois.json`). Worlds Edge,
  Olympus and Broken Moon are current; E-District, Kings Canyon, Storm Point still have old spreadsheet
  images. Wildcard maps need this once.
* **Screen stops being recognised** (UI update): `Build_References.py` with a fresh screenshot.
* **Re-read a match that still has its screenshots**: `.venv/bin/python Extract_Stats.py Captures/<match>`
  (`--force` = all, `--no-sheet` = don't touch the workbook, `--clean` = delete screenshots of read matches).
* **Old recording**: `.venv/bin/python Stat_Capture.py --video file.mp4` (stays out of the workbook).
* **Heatmaps by hand**: `.venv/bin/python Heatmap.py ["Olympus"] [--me]`.

## Files

| | |
|---|---|
| `Stat_Capture.py` | the background capture (OBS WebSocket, screen tracking, what to save, live map, starts the reading) |
| `Screen_Detection.py`, `Screen_References/` | screen recognition; `screens.json` = anchors, crop boxes, save rules |
| `Build_References.py` | cut new anchor crops from a screenshot |
| `Extract_Stats.py` | reads a match into stats.json / CSV rows; clean-up; settings at the top |
| `Map_Locate.py` | minimap → map positions, POI / Death Zone / Contested, rings, map pictures |
| `Match_Map.py` | drawing (match pictures, SVG), live map, squad colours, scans, rings on the full map |
| `Heatmap.py` | all-games heatmaps and ring end zones |
| `Sheet_Write.py`, `Calc_Macro.py` | workbook writing (the macro runs inside headless LibreOffice) |
| `restart_capture.sh` | (re)start the service |
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
