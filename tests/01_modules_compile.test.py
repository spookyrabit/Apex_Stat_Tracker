import ast, sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
bad = 0
for f in sorted(root.glob("*.py")):
    try:
        ast.parse(f.read_text(), str(f))
    except SyntaxError as e:
        print(f.name, e); bad = 1
sys.exit(bad)
