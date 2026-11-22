import json, sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent / "Maps"
bad = 0
for p in sorted(root.glob("*/pois.json")):
    d = json.loads(p.read_text())
    w, h = d["reference_size"]
    for poi in d["pois"]:
        if not (0 <= poi["x"] <= w and 0 <= poi["y"] <= h):
            print(p.parent.name, poi["name"], "outside the map"); bad = 1
sys.exit(bad)
