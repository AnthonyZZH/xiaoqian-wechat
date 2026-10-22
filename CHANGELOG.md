# Changelog

Each update to this repository is tagged as a small version (`v0.1`, `v0.2`, …).

## v0.2

- The designated contact's private chat can hand tasks to local command-line agents (Claude Code, Codex, DeepSeek Harness), list and stop them, with an optional passphrase; finished tasks come back as a one-line report.
- Owner sends: “给某某发：…” or “在某群里说：…” posts the text to any WeChat chat found by name, without the model, and reports truthfully whether it went out. Short group names are used only when they match one chat.
- Multi-chat scheduling and explicit relay recipients (`routing.py`).
- Notices that were definitely not sent are retried; uncertain sends never are.
- Commands from a pause, or the last old message on `--reply-latest`, are recorded as context only.

## v0.1

- First sanitized release: personas, long-term memory, stickers, web search, multi-chat monitoring, and the Tk console.
