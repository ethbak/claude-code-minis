#!/usr/bin/env python3
"""Copies shared/claude_env.py into every plugin's scripts/. With --check, exits 1 if any copy differs."""
import filecmp, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "shared" / "claude_env.py"
copies = [p / "scripts" / "claude_env.py" for p in sorted((ROOT / "plugins").iterdir()) if p.is_dir()]
stale = [c for c in copies if not c.exists() or not filecmp.cmp(SOURCE, c, shallow=False)]
if "--check" in sys.argv:
    for c in stale:
        print(f"{c.relative_to(ROOT)} differs from shared/claude_env.py; run tools/sync_shared.py")
    sys.exit(1 if stale else 0)
for c in stale:
    shutil.copyfile(SOURCE, c)
    print("updated", c.relative_to(ROOT))
