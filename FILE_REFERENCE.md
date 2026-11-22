# File reference

## Find by feature
- Screen capture loop (OBS, saves screenshots) → Stat_Capture.py, Screen_Detection.py, Screen_References/screens.json, restart_capture.sh
- Detection references / templates → Build_References.py, TemplateCreator.py, Frame_Templates/*.json, Screen_References/
- Reading stats from screenshots → Extract_Stats.py, Legend_Icons.py, Check_Reads.py
- Workbook writing (LibreOffice .ods) → Sheet_Write.py, Calc_Macro.py, Seasons.py, Past_Seasons.py
- Map position, paths, POI, death zone → Map_Locate.py, Map_Icons.py, Maps/<Map>/pois.json
- Match picture / OBS live map → Match_Map.py, Map_View.py
- Heatmap, rings, movement → Heatmap.py, Ring_Predict.py, Movement.py
- Window (stats, capture controls) → Control_Panel.py, Screen_Notes.py
- Manual screenshot sorting → Extract_Screenshots.py, Image_Sorting.py
- Setup and usage → Readme.md, requirements.txt

## Root (all Python, run with .venv/bin/python)
| File | What it does |
|---|---|
| Stat_Capture.py | Background capture: watches OBS, saves stat screenshots and minimap crops into Captures/<match>/ |
| Screen_Detection.py | Works out which Apex screen is showing (matches Screen_References/screens.json) |
| Build_References.py | Cuts reference crops for Screen_Detection from full screenshots |
| TemplateCreator.py | Draw rectangles on a frame to make Frame_Templates/*.json |
| Image_Sorting.py | Older OCR-based sorter of screenshots by frame template |
| Extract_Screenshots.py | Tk tool to pull screenshots out of videos |
| Extract_Stats.py | Reads stats from a match's screenshots into stats.json, Sheet Rows.csv, then triggers map/sheet/heatmap work |
| Check_Reads.py | Regression check: re-reads every match's summary and lists changed reads |
| Legend_Icons.py | Legend per player from squad-panel portraits (fallback) |
| Screen_Notes.py | Draws what the readers measured on a match's screenshots (Latest tab) |
| Map_Locate.py | Matches minimaps to the map: path, landing POI, death zone, contested, rings |
| Map_Icons.py | Finds map icons (care packages, harvesters...) and removes them from map pictures |
| Map_View.py | Maps-tab layers at any zoom, SVG export |
| Match_Map.py | Per-match map picture and LiveMap (writes Latest Match Map*.png for OBS) |
| Heatmap.py | All-games heatmap and ring layers per map |
| Ring_Predict.py | Predicts endgame ring location from rings seen and past games |
| Movement.py | Movement stats (glide, distance, squad spread) from trails.json |
| Sheet_Write.py | Writes finished matches into the season .ods workbook |
| Calc_Macro.py | Python macro run inside LibreOffice; does the workbook edits |
| Seasons.py | Season of a match and where its workbook lives (SEASON_STARTS) |
| Past_Seasons.py | Merges old Google Sheets exports into Past_Seasons/past_games.csv |
| Control_Panel.py | The Tk window: stats tabs, maps tab, capture controls |
| restart_capture.sh | (Re)starts the capture as a systemd user service |
| requirements.txt | Python dependencies |

## Data folders
| Folder | What it holds |
|---|---|
| Screen_References/ | Fixed reference crops (png) and screens.json; legend_icons/, map_icons/, rank_numerals/ |
| Frame_Templates/ | One JSON per screen type (rectangles for OCR) |
| Maps/<Map>/ | pois.json and reference_base.png (fixed); layer_* and info are generated |
| Captures/, Sorted/, Past_Seasons/ | Generated/local data, git-ignored |
