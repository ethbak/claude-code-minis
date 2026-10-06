#!/usr/bin/env bash
# Nightly live Remote Control test on a maintainer's machine (GitHub's runners have no claude.ai login that lasts):
# tests what is on main from a clone of its own, and opens or updates a GitHub issue when it fails.
# Run by a launchd agent or a cron job; uses this machine's claude.ai login.
set -uo pipefail
repo=ethbak/claude-code-minis
home="${MINIS_LIVE_HOME:-$HOME/.cache/claude-code-minis}"
checkout="$home/checkout"
log="$home/last-run.log"
mkdir -p "$home"

{
  echo "== $(date '+%F %T') $(claude --version 2>&1)"
  if [ -d "$checkout/.git" ]; then
    git -C "$checkout" fetch -q origin main && git -C "$checkout" reset -q --hard origin/main
  else
    gh repo clone "$repo" "$checkout" -- -q
  fi
  echo "main at $(git -C "$checkout" rev-parse --short HEAD)"
  cd "$checkout" && MINIS_LIVE=1 uv run --with pytest pytest tests/test_live_remote_control.py -q -rA 2>&1
} > "$log" 2>&1
status=$?
cat "$log"
[ "$status" -eq 0 ] && exit 0

version=$(claude --version 2>/dev/null | cut -d' ' -f1)
# The issue is public: drop session, environment and account ids, and the home folder path.
redacted=$(tail -c 60000 "$log" | sed -E "s#$HOME#~#g; s#$USER#<user>#g; s#(cse|env|session|org|user)_[A-Za-z0-9]{8,}#\1_…#g; s#[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}#<uuid>#g")
body=$(printf 'The nightly live Remote Control test failed with Claude Code %s.\n\n```\n%s\n```\n' "$version" "$redacted")
open=$(gh issue list -R "$repo" --state open --label live-nightly --json number -q '.[0].number' 2>/dev/null)
if [ -n "$open" ]; then
  gh issue comment "$open" -R "$repo" --body "$body"
else
  gh label create live-nightly -R "$repo" --color B60205 --description "Nightly live Remote Control test" 2>/dev/null
  gh issue create -R "$repo" --label live-nightly --title "Live Remote Control test failed (Claude Code $version)" --body "$body"
fi
exit "$status"
