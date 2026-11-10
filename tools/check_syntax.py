import ast
import io
import sys
from pathlib import Path

root = Path(sys.argv[1])
bad = 0
count = 0
for p in sorted(root.rglob("*.py")):
    s = str(p)
    if ".venv" in s or "__pycache__" in s:
        continue
    count += 1
    try:
        ast.parse(io.open(p, encoding="utf-8").read(), filename=s)
    except Exception as e:
        bad += 1
        print("FAIL", p.name, type(e).__name__, e)
print("checked", count, "files, failures =", bad)
