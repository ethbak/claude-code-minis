#!/usr/bin/env python3
"""Installs (or with --uninstall removes) the remote-terminal helper as a root service:
a LaunchDaemon on macOS, a systemd unit on Linux. Run with sudo.

The helper script is copied to a root-owned directory, so the service never runs a file the user can change."""
import os, platform, shutil, subprocess, sys, time
from pathlib import Path

MACOS = platform.system() == "Darwin"
LABEL = "io.github.ethbak.remote-terminal-helper"
TARGET_DIR = Path("/usr/local/libexec/remote-terminal-helper")
TARGET = TARGET_DIR / "helperd.py"
PYTHON = "/usr/bin/python3"
PLIST = Path(f"/Library/LaunchDaemons/{LABEL}.plist")
UNIT = Path("/etc/systemd/system/remote-terminal-helper.service")
SOCKET = "/var/run/remote-terminal-helper.sock" if MACOS else "/run/remote-terminal-helper.sock"
LOG = "/var/log/remote-terminal-helper.log"


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def install_files():
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parent / "helperd.py", TARGET)
    for path in (TARGET_DIR, TARGET):
        os.chown(path, 0, 0)
    TARGET_DIR.chmod(0o755)
    TARGET.chmod(0o644)


def install_macos():
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{PYTHON}</string><string>{TARGET}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict>
</plist>
""")
    os.chown(PLIST, 0, 0)
    PLIST.chmod(0o644)
    run("launchctl", "bootout", f"system/{LABEL}", check=False)
    for _ in range(50):  # bootstrap fails while the old job is still being torn down
        if run("launchctl", "print", f"system/{LABEL}", check=False).returncode != 0:
            break
        time.sleep(0.1)
    run("launchctl", "bootstrap", "system", str(PLIST))


def install_linux():
    if not shutil.which("systemctl"):
        sys.exit("No systemd here. Run the helper as root some other way: "
                 f"{PYTHON} {TARGET} (it logs to stderr).")
    UNIT.write_text(f"""[Unit]
Description=remote-terminal helper for Claude Code (reports whether a terminal waits for input)

[Service]
ExecStart={PYTHON} {TARGET}
Restart=always

[Install]
WantedBy=multi-user.target
""")
    UNIT.chmod(0o644)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "remote-terminal-helper.service")
    run("systemctl", "restart", "remote-terminal-helper.service")


def uninstall():
    if MACOS:
        run("launchctl", "bootout", f"system/{LABEL}", check=False)
        PLIST.unlink(missing_ok=True)
    elif shutil.which("systemctl"):
        run("systemctl", "disable", "--now", "remote-terminal-helper.service", check=False)
        UNIT.unlink(missing_ok=True)
        run("systemctl", "daemon-reload", check=False)
    shutil.rmtree(TARGET_DIR, ignore_errors=True)
    Path(SOCKET).unlink(missing_ok=True)
    print("remote-terminal helper removed")


def main():
    if os.geteuid() != 0:
        sys.exit(f"Run with sudo: sudo python3 {Path(__file__).resolve()}" + (" --uninstall" if "--uninstall" in sys.argv else ""))
    if "--uninstall" in sys.argv:
        uninstall()
        return
    # On macOS /usr/bin/python3 always exists, but without the Command Line Tools it only offers to install them.
    if MACOS and run("/usr/bin/xcode-select", "-p", check=False).returncode != 0:
        sys.exit(f"{PYTHON} needs the Xcode Command Line Tools. Install them with: xcode-select --install")
    if not Path(PYTHON).exists():
        sys.exit(f"{PYTHON} not found. Install Python 3 from your distribution's packages.")
    install_files()
    install_macos() if MACOS else install_linux()
    for _ in range(50):
        if Path(SOCKET).exists():
            print(f"remote-terminal helper running ({SOCKET})")
            return
        time.sleep(0.1)
    sys.exit(f"The helper did not start. See {LOG}" if MACOS else
             "The helper did not start. See: journalctl -u remote-terminal-helper")


if __name__ == "__main__":
    main()
