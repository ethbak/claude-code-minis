import json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
sys.path.insert(0, str(ROOT / "shared"))


def run_hook(script, payload, env=None, timeout=330):
    """Runs a hook script the way Claude Code does: JSON on stdin. Returns the parsed JSON reply, or None if the
    hook printed nothing."""
    out = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True,
                         env=env, timeout=timeout)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None
