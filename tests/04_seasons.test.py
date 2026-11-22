import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import Seasons
for s, start in Seasons.SEASON_STARTS.items():
    assert Seasons.season_at(start) == s, s
assert Seasons.season_of_match(start.strftime("%Y-%m-%d_%H-%M-%S")) == s
