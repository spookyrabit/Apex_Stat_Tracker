import json, sys
from pathlib import Path
d = Path(__file__).resolve().parent.parent / "Screen_References"
cfg = json.loads((d / "screens.json").read_text())
missing = []
for name, s in cfg["screens"].items():
    for a in s["anchors"]:
        if not (d / f"{name}__{a}.png").exists():
            missing.append(f"{name}__{a}.png")
print("missing:", missing)
sys.exit(1 if missing else 0)
