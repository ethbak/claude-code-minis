---
name: rresume
description: List, search or resume Claude Code sessions from Remote Control, where /resume is unavailable. Brings back archived chats and moves terminal sessions into Remote Control.
argument-hint: "[n | next | prev | search text]"
disable-model-invocation: true
---

The remote-resume hook already handled `/rresume $ARGUMENTS` and attached its result as context. Follow that context. If no remote-resume context is present, tell the user the remote-resume hook did not run, and suggest `/plugin` to check that the remote-resume plugin is enabled.
