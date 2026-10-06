---
name: mode
description: Switch this Remote Control session's permission mode (default, accept, plan, auto, dontask, bypass). Reaches auto and bypassPermissions, which the app's menu doesn't offer.
argument-hint: "default | accept | plan | auto | dontask | bypass"
disable-model-invocation: true
---

The mode-picker hook already handled `/mode $ARGUMENTS` and attached its result as context. Follow that context. If no mode-picker context is present, tell the user the mode-picker hook did not run, and suggest `/plugin` to check that the mode-picker plugin is enabled.
