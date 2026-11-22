#!/usr/bin/env python3
"""The one test runner. Each test is its own file tests/NN_name.test.py (a script: exit 0 = pass).

    python3 tests/run_all.py            all tests -> "X Tests Pass X Fail" + names of failed ones
    python3 tests/run_all.py <part>     only tests whose file name contains <part>, full output shown
Full log of the last run: tests/last_run.log
"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
part = sys.argv[1] if len(sys.argv) > 1 else None
files = sorted(p for p in HERE.glob("*.test.py") if part is None or part in p.name)
log, failed = [], []
for f in files:
    r = subprocess.run([sys.executable, str(f)], cwd=ROOT, capture_output=True, text=True)
    out = f"=== {f.name}: {'PASS' if r.returncode == 0 else 'FAIL'}\n{r.stdout}{r.stderr}"
    log.append(out)
    if r.returncode:
        failed.append(f.name)
    if part:
        print(out)
(HERE / "last_run.log").write_text("\n".join(log))
print(f"{len(files) - len(failed)} Tests Pass {len(failed)} Fail")
for n in failed:
    print("FAILED:", n)
sys.exit(1 if failed or not files else 0)
