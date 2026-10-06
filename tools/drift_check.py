#!/usr/bin/env python3
"""Checks the installed Claude Code binary still has the internals these plugins rely on. A missing marker means a
release renamed or removed something; the plugin that uses it needs a look before users hit it.
Usage: tools/drift_check.py [path to claude]   (default: the claude on PATH)"""
import os, re, shutil, sys
from pathlib import Path

MARKERS = {
    "all": ["permission_mode", "transcript_path", "claudeAiOauth", ".credentials.json", "-credentials",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR", "--session-id", "/v1/code/sessions", "control_response"],
    "mode-picker": ["set_permission_mode"],
    "remote-resume": ["bridge/reconnect", "worker/internal-events", "/worker/events", "apply_flag_settings",
                      "set_cwd", "needs_trust", "trusted_directory", "3ab19d7e-9f35-45c2-926e-75e271cc60b3",
                      "environments-2025-11-01", "CLAUDE_CODE_ENVIRONMENT_KIND", "custom-title", "ai-title",
                      "permission-mode", "stop_hook_summary", "organizationUuid"],
}
# Built-in commands that would shadow ours.
SHADOWED = re.compile(rb'name:"(mode|rresume)"')


def main():
    found = sys.argv[1] if len(sys.argv) > 1 else shutil.which("claude")
    if not found:
        sys.exit("claude not found")
    binary = Path(os.path.realpath(found))
    data = binary.read_bytes()
    problems = [f"{plugin}: marker {m!r} is gone" for plugin, markers in MARKERS.items()
                for m in markers if m.encode() not in data]
    problems += [f"a built-in /{m.decode()} command now exists and shadows ours" for m in set(SHADOWED.findall(data))]
    print(f"checked {binary}")
    for p in problems:
        print("DRIFT", p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
