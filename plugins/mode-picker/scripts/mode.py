#!/usr/bin/env python3
"""UserPromptSubmit hook behind /mode. The claude.ai apps offer a Remote Control session at most Manual, Accept edits
and Plan; this sends the session the same set_permission_mode control request for any of the six modes, and waits for
the session to confirm it."""
import json, re, sys, time, urllib.error, uuid
from claude_env import Problem, answer, api, http_problem, remote_session_id

# Claude Code's mode id, the word you type, the name and what it means; in menu order.
MODES = [
    ("default", "default", "Default", "asks before edits and commands"),
    ("acceptEdits", "accept", "Accept edits", "edits files without asking"),
    ("plan", "plan", "Plan", "reads and plans, no edits"),
    ("auto", "auto", "Auto", "a classifier reviews actions instead of you"),
    ("dontAsk", "dontask", "Don't ask", "denies anything not pre-approved"),
    ("bypassPermissions", "bypass", "Bypass permissions", "runs everything without asking"),
]
BY_ID = {m[0]: m for m in MODES}
WORDS = {**{m[1]: m[0] for m in MODES}, "ask": "default", "acceptedits": "acceptEdits", "edits": "acceptEdits",
         "dont-ask": "dontAsk", "bypasspermissions": "bypassPermissions", "yolo": "bypassPermissions"}
CONFIRM_SECONDS = 15


def label(mode):
    return BY_ID[mode][2] if mode in BY_ID else mode


def finish(cse, text, markdown=None):
    answer(cse, text, markdown)
    sys.exit(0)


def menu(current, unknown=None):
    """(plain, markdown) for /mode alone or an unknown mode: the current mode and every choice."""
    plain = (f'Unknown mode "{unknown}". ' if unknown else "") + \
        f"Usage: /mode default | accept | plan | auto | dontask | bypass. Current mode: {current}."
    rows = ["| Command | Mode | What it does |", "|---|---|---|"]
    for mode, word, name, meaning in MODES:
        name = f"**{name}** (current)" if mode == current else name
        rows.append(f"| `/mode {word}` | {name} | {meaning[:1].upper()}{meaning[1:]} |")
    intro = f'Unknown mode "{unknown}". Choose one of these:' if unknown else f"Current mode: **{label(current)}**"
    return plain, intro + "\n\n" + "\n".join(rows)


def set_mode(cse, mode):
    """Sends the request and returns the session's control_response, or None if none came."""
    request_id = str(uuid.uuid4())
    api("POST", f"/code/sessions/{cse}/events", {"events": [{"payload": {
        "type": "control_request", "request_id": request_id, "uuid": str(uuid.uuid4()), "session_id": cse,
        "request": {"subtype": "set_permission_mode", "mode": mode}}}]})
    deadline = time.time() + CONFIRM_SECONDS
    while time.time() < deadline:
        for event in api("GET", f"/code/sessions/{cse}/events?limit=50").get("data", []):
            payload = event.get("payload") or {}
            response = payload.get("response") or {}
            if payload.get("type") == "control_response" and response.get("request_id") == request_id:
                return response
        time.sleep(0.5)
    return None


def not_changed(cse, reason):
    finish(cse, f"Mode not changed: {reason}.", f"**Mode not changed**\n\n> {reason[:1].upper()}{reason[1:]}.")


def main():
    data = json.load(sys.stdin)
    command = re.fullmatch(r"\s*/(?:mode-picker:)?mode(?:\s+(\S+))?\s*", data.get("prompt", ""))
    if not command:
        return
    current, cse = data.get("permission_mode", "unknown"), remote_session_id()
    word = (command.group(1) or "").lower()
    if word not in WORDS:
        finish(cse, *menu(current, word or None))
    mode = WORDS[word]
    if not cse:
        finish(cse, "/mode works in Remote Control sessions. In a terminal, press Shift+Tab or start Claude Code with "
               "--permission-mode.")
    try:
        response = set_mode(cse, mode)
    except Problem as problem:
        not_changed(cse, str(problem))
    except (urllib.error.URLError, OSError) as error:
        not_changed(cse, http_problem(error))
    if response is None:
        finish(cse, f"Sent the switch to {mode}, but the session did not confirm it within {CONFIRM_SECONDS} s. "
               "Check the mode shown in the app.",
               f"**Not confirmed**\n\n> Sent the switch to {label(mode)}, but the session didn't confirm it within "
               f"{CONFIRM_SECONDS} seconds. Check the mode shown in the app.")
    if response.get("subtype") != "success":
        not_changed(cse, response.get("error") or "the session refused it")
    meaning = BY_ID[mode][3]
    finish(cse, f"Switched to {mode} (was {current}).",
           f"Switched to **{label(mode)}** from {label(current)}.\n\n> {meaning[:1].upper()}{meaning[1:]}.")


if __name__ == "__main__":
    main()
