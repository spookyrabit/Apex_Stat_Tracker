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