# Changelog

Each update to this repository is tagged as a small version (`v0.1`, `v0.2`, …).

## v0.4

- Opening topics on her own (`proactive.py`): a bubble in a group after it has been quiet for a while, and a message to the designated contact every few hours. When a chat is due she is only asked to consider it; the model may stay silent, which reschedules without counting.
- Pacing per chat with random windows, a group silence threshold, quiet hours (also across midnight), an unanswered limit that resets when someone speaks, and state that survives restarts.
- Console switches for "message the owner" and "bubble in groups", and quiet hours for each.
- Replies get a 2000-token budget and openings 4000, so long-thinking local models are not cut off.

## v0.3

- Real @mentions in groups: the member is picked from WeChat 4.1's mention popover (the approach of pyweixin); members it lacks go out as plain "@name" and are reported.
- Free-worded owner requests ("在群里艾特我，说：…") are read by the local model, then the chat and @ targets are checked before anything is sent; short group names and "我" are understood.
- Blocking: "拉黑 X" / "不允许再接收@X 的消息" / "解除拉黑 X" / "黑名单", also from the owner @-ing her in a group.
- Memory: "总结记忆" updates it on request; updates no longer fail on models that think for thousands of tokens, and work after a restart.
- Stickers first: at most one small emoji per reply, none reused from her last 4 replies, a sticker suggested after 2 plain replies, sticker numbers never leak into text.

## v0.2

- The designated contact's private chat can hand tasks to local command-line agents (Claude Code, Codex, DeepSeek Harness), list and stop them, with an optional passphrase; finished tasks come back as a one-line report.
- Owner sends: “给某某发：…” or “在某群里说：…” posts the text to any WeChat chat found by name, without the model, and reports truthfully whether it went out. Short group names are used only when they match one chat.
- Multi-chat scheduling and explicit relay recipients (`routing.py`).
- Notices that were definitely not sent are retried; uncertain sends never are.
- Commands from a pause, or the last old message on `--reply-latest`, are recorded as context only.

## v0.1

- First sanitized release: personas, long-term memory, stickers, web search, multi-chat monitoring, and the Tk console.
