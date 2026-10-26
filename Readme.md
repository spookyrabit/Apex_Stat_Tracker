Stat Capture (what runs today)

Stat_Capture.py runs in the background while you play. 3 times a second it asks OBS for a small
screenshot of the "Capture Card" source (it never opens the capture card itself, so OBS keeps recording),
figures out which Apex screen is showing, and only keeps the screenshots that hold stats:

waiting_first.jpg / waiting_last.jpg – lobby: mode, map, players skill distribution graph
legend_select_last.jpg – final legend picks (your legend + teammates, mode, map)
your_squad_last.jpg – squad names, badges, mode and map
dropship_first.jpg / dropship_last.jpg – compass heading on the dropship
HHMMSS_post_game_NN.jpg – one shot per end-of-match page you look at (Summary, Death Recap, Experience...;
                          not the LIVE/spectate tab)
post_game_last.jpg – the last end-of-match frame (fully animated in)
minimap/HHMMSS.jpg – minimap crop every 15 s during the match (landing / death location)
match.json – timeline of every screen seen and when

Each match gets its own folder in Captures/.

Setup (once)

1. python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
2. OBS → Tools → WebSocket Server Settings → tick "Enable WebSocket server" (port 4455).
   Click "Show Connect Info" to copy the password.
3. Optional: obs_settings.json (gitignored) next to Stat_Capture.py, e.g. {"source": "Capture Card"}.
   The WebSocket password is read from OBS's own config automatically.

Background service (keeps running while you play; survives OBS closing):
./restart_capture.sh                 start / restart it now
./restart_capture.sh --after-match   restart once the current match is in the workbook (after code changes)
systemctl --user status apex-stat-capture    is it running?      tail -f Captures/capture.log    what it sees

Run

.venv/bin/python Stat_Capture.py                      (live; waits for OBS if it isn't open yet)
.venv/bin/python Stat_Capture.py --verbose            (prints detection scores every poll)
.venv/bin/python Stat_Capture.py --video "file.mp4"   (backfill from an old OBS recording)

Reading the stats (Extract_Stats.py)

Runs by itself (low priority) after each match's summary screen, or by hand:
.venv/bin/python Extract_Stats.py            (new matches only; add --force to redo all)

Writes stats.json into each match folder, adds new matches to the workbook (below), and rebuilds:
Captures/Sheet Rows.csv  – one row per match, same columns (A..AH) as the Past Games sheet.
                           Filled: placement, squad kills, kills/assists/knocks, time, revives, respawns,
                           damage, legend, end time, Most Damage?, mode, map, teammate legends, ranked entry,
                           dropship heading, EST XP, percentile (skill graph: band start-your team-band end),
                           skill, squad. Left blank: death zone, POI, contested, friends, RP columns.
Captures/Squad Stats.csv – one row per player per match (you and teammates): legend, K/A/K, damage,
                           survival time, revives, respawns.
Set PLAYER_NAME (your gamertag) and FRIENDS (gamertag -> Friends column) at the top of Extract_Stats.py.

Spreadsheet (Sheet_Write.py) – local, offline, LibreOffice Calc

Apex Game Data S30.ods is the workbook. After each match the row goes into the next empty row of
"Past Games" (only the data cells; the formula columns EST XP, Skill, Squad and the RP columns are left
to the workbook, and Death Zone / POI / Contested are never overwritten), and every player on the squad
goes into "Squad Stats" (filter Is Me = Yes for just your own games). LibreOffice itself does the editing
(Calc_Macro.py, run headless with its own private profile in Captures/), so your dashboards are safe.

If the workbook is open in Calc when a match ends, nothing is written; the match waits and goes in within
a minute of you closing it. Matches you typed in by hand are recognised and skipped. Matches where the
placement couldn't be read are left for you to type in. Log: Captures/libreoffice.log

.venv/bin/python Sheet_Write.py              (add any matches that aren't in the workbook yet)
.venv/bin/python Sheet_Write.py --convert    (one-time: built the .ods from the Google Sheets .xlsx)
.venv/bin/python Sheet_Write.py --refresh    (rewrite rows the scripts wrote with the latest readings; never
                                              blanks a cell, never touches rows you typed in)
.venv/bin/python Sheet_Write.py --rebuild-squad --fill-in   (start Squad Stats over; fill empty DZ/POI/Contested)

Map positions, POI, Death Zone, Contested (Map_Locate.py)

During the dropship and the match, the minimap is saved every second (plus the kill feed every 2 s for the
first 4 minutes, and the SQUADS LEFT counter every 5 s). After the match every minimap is matched against
the map picture in Maps/<Map>/reference.png, which gives your path (Captures/<match>/path.jpg), the
dropship's flight path, where you landed (POI = nearest POI in Maps/<Map>/pois.json) and where you died
(Death Zone). Contested = someone in your squad shows up in the kill feed within 2 minutes of landing
(a fight with no knocks can be missed - change it by hand if so). Each match also adds its minimaps to
Maps/<Map>/stitched.png, a higher-res map that fills in as you play.

These go into Past Games (Death Zone, POI, Contested - only if the cell is empty) and Squad Stats.
Fix a wrong value for a match in Captures/<match>/fixes.json, e.g. {"poi": "The Core", "contested": "Yes"},
then run Extract_Stats.py --force and Sheet_Write.py --rebuild-squad --fill-in.

Match map picture + OBS (Match_Map.py)

Every match gets Captures/<match>/match_map.png: the map (sharpened by the stitched minimaps) with each
squad member's trail in their squad colour (names read from the squad panel), the dropship's flight,
where you landed (green circle) and died (red X), and the ring every time you opened the full map (white =
ring, blue = next ring). The newest one is always copied to "Latest Match Map.png" in this folder.
During a match it's redrawn every 5 seconds from the live minimaps ("LIVE"), then replaced by the full
version a minute or two after the stats screen.

Rings: every time you open the full map the ring is measured - white = the ring, grey = the next ring,
each labelled with the time it was seen. The map underneath gets cleaner over time: every full-map
screenshot is lined up with Maps/<Map>/reference.png and kept in Maps/<Map>/screens/, and the picture is
rebuilt as the per-pixel median of them (reference_base.png is the original), so old rings, red tint, the
dropship line and the cursor fade out the more often you open the map. Teammate arrows sitting on a map
icon of the same colour (green respawn beacons, orange lava) are ignored.

Each match also gets match_map_zoom.png ("Latest Match Map Zoomed.png"): a close-up of where the squad
went after landing, with a dot on your trail every minute after the jump (1m, 2m, ...). Teammate trails
are smoothed so a ping near them doesn't make the line jump.

Vector version for zooming right in: Captures/<match>/match_paths.svg, on the map picture's own coordinates,
with separate layers (SVG groups): map (the picture, linked from Maps/ - hide it or swap it), rings, dropship,
players (one group per player; every line carries the time of each point, plus a dot each minute), events
(landing, death) and pois. Lines stay the same thickness however far you zoom. The same data is in
Captures/<match>/trails.json (players, rings, dropship, landing, death, POIs) for building a UI on later.

Rings come from two places: the full map whenever you open it (the timer in its left panel says whether the
ring is closing - a ring caught mid-close is drawn dashed orange as "Closing", a holding ring is "Ring <round>"),
and the minimap: when you're near the edge the red tint shows an arc of it, and arcs that fit the same circle
for 20 s+ are a ring that's holding still. That picks up the late rings even if you never open the map.
Each map's ring sizes are learned as you play (Maps/<Map>/info.json "ring_sizes"), so a ring that's only
partly on a zoomed-in map screenshot can still be placed. Trails don't join across gaps (dead, respawned).
Teammates are only followed if the end card / legend select says you had them (a solo game has no trails).
Spectating a teammate is captured too (the HUD moves down while spectating); spectating your killers after
your squad is out isn't. If the service restarts mid-match, the live picture replays what was saved so far.

During the match a full screenshot is also kept every 45 s (Captures/<match>/full/), plus screenshots of the
screens we don't recognise right after legend select (the last pick's reveal) and after the match (a win's
Champion screen). The full map opened while dead (no button hints along the bottom) is recognised by lining
the middle of the screen up with the map picture.

Death Zone is where the last surviving member of your squad went down (the minimap follows the teammate you
spectate). Most Damage? is your share of the squad's damage, as a percentage.

Contested (another squad landed at your POI) can't be seen directly, so it's a best guess: Yes when, from
15 s before to 2 min after landing, the kill feed shows "Squad landed nearby!" (the game's own call-out),
someone in your squad spotted an enemy, broke an enemy's shield, or fought someone. Checked against 26 games
recorded by hand it agreed 23 times. stats.json keeps the reasons and the exact lines (location ->
contested -> evidence). It only fills an empty cell, so type over it whenever you know better.

OBS: add an Image source, browse to "Latest Match Map.png" in this folder. OBS reloads it when it changes.
(Matches on a map whose picture doesn't match the game leave the last picture in place.)

If a map's picture doesn't match the game (a map update), open the full map zoomed out in the dropship
and run: .venv/bin/python Map_Locate.py --reference-from Captures/<match>/<..._map_01.jpg> --map "Worlds Edge"
Edit Maps/<Map>/pois.json to move or add POIs.

If you requeue before the stats screen, the match still gets a row: placement from the last SQUADS LEFT
seen, legends and map from legend select, POI / Death Zone from the minimap, Stats Screen = Missed.

If a screen stops being detected (Apex UI update), grab a screenshot of it and run
.venv/bin/python Build_References.py <screen> screenshot.png
Screen names and the anchor boxes they check are in Screen_References/screens.json.

----------------------------------------------------------------------------------------------------

Overview

This project defines a computer-vision–driven system designed to extract detailed Apex Legends Ranked match data from gameplay video in real time.
The system does not interact with the game and does not automate gameplay.
It only observes the screen and records information for post-match statistical analysis.

The objective is to build a reliable, automated data-collection pipeline that documents:

Legend selections

Team composition

Player color (squad position)

Full death-screen statistics for all squad members

Minimap-based movement paths

Ring locations and ring movement timing

POI-based survival performance

Early/mid/late-game position outcomes

All collected data is analyzed only after the match has ended.

Core Design Principles

Vision-only system
The tool reads frames from a live gameplay video stream (e.g., OBS virtual camera, screen capture feed).

Screen-state classification
The system must identify which Apex UI screen is currently displayed (Legend Select, Dropship, Skydive, Gameplay, Death Screen).

Non-intrusive
No inputs are sent to the game. No gameplay automation.

Match-end aggregation
All stats are stored and processed once the match has concluded.

Consistency requirements
Several Apex settings must remain fixed for stable recognition (see Constraints).

Tracked Stats & How They Are Extracted
1. Legend Selection Screen

Purpose: determine team composition and player identity.

Data extracted:

Player squad color (Blue = 1st pick, Orange = 2nd pick, Green = 3rd pick)

Player-selected legend

Teammates’ selected legends

Any visible banners, badges, or trackers for context (optional)

Detection method:

UI template matching to detect the Legend Select screen

OCR/vision to read legend names or identify portraits

Color detection to identify player panel highlight color

2. Dropship → Skydive (Optional)

Useful to correlate drop path with outcome.

Data extracted:

Dropship path direction (screen overlay arrow)

Squad jump timing (early, mid, late)

Drop location (approximate minimum bounding box around skydive vector)

Detection method:

UI state recognition (“Hold E to launch solo”, etc.)

Minimap observation during descent

Matching known POI regions from the map templates

3. Minimap-Based Movement Tracking

Purpose: reconstruct pathing and correlate survival performance with map zones.

Data extracted:

Player movement path across the map

Time spent at each POI

Rotation timing

Engagement zone (where first contact occurs)

Ring positions and player proximity to ring

Detection method:

Minimap crop from gameplay frames

Icon tracking via template matching

POI overlay mapping with predefined coordinates

Ring circle detection using color/shape thresholds

Requirements:

Minimap rotation must be OFF to maintain consistent north-up tracking

HUD scaling must remain consistent across sessions

4. Combat & Engagement Tracking (Optional Future Expansion)

Possible data points:

First damage timing

Direction of attack (approximate from damage indicator UI)

Number of engagements before death

Whether player was first knocked or last survived

Detection method:
High-speed UI parsing of damage indicators, knock banners, and hit markers.

5. Death Screen Stat Capture

This is the most critical component.

Data extracted (per player):

Damage dealt

Damage taken

Knocks / assists

Survival time

Placement

Legend played

Total squad performance

Final ring location (from minimap)

Detection method:

Death screen UI template match

OCR to extract numerical values

Mapping the stats into a structured record

6. Post-Match Summary Data

Data aggregated only after match end:

Full match timeline

POIs visited

Time alive per POI

Distance traveled per phase

First fight outcome and location

Ring progression and player positioning

Team composition vs. placement correlations

Personal performance metrics (damage per minute, knock conversion rate)

Constraints & Required Game Settings

To ensure reliable data extraction:

Mandatory

Minimap Rotation: OFF

HUD Scaling: Fixed (default recommended)

Resolution: Fixed (1080p or 1440p recommended for stable OCR)

Colorblind Mode: OFF (or a specified mode, but must be consistent)

Brightness/Gamma: Consistent

Recommended

Disable unnecessary HUD clutter

Maintain consistent OBS scaling (no resampling)

System Flow Summary

Frame Capture
Live video frames acquired via screen capture or virtual camera.

Screen State Detection
System switches parsers depending on whether the game is in
Legend Select, Gameplay, or Death Screen.

Real-Time Data Collection
Minimap tracking, legend parsing, movement recording.

Match End Trigger
Triggered by detecting the death/summary screen.

Data Consolidation
All raw detections combined into a single match record.

Output Storage
JSON/SQLite/CSV depending on configuration.

Planned Modules

/capture/ – raw frame capture

/detect/ – screen state, legends, stats

/map/ – POI mapping, minimap parser

/ocr/ – text extraction

/pipeline/ – match session manager

/storage/ – result database

/analysis/ – scripts for POI performance + survival modeling

Future Expansion Ideas

Loadout recognition (weapons, attachments)

Enemy composition detection

Fight-to-fight path reconstruction

Compare Ranked vs. pubs performance automatically

Predictive modeling:
“Given your first ring position + legend + team comp, your expected placement is: X”