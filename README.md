# Xiaoqian · Local-LLM AI WeChat Group Chat Bot

English | [简体中文](README.zh-CN.md)

Xiaoqian (小千) is an AI WeChat chatbot and group-chat companion for Windows, powered by a local LLM through LM Studio (e.g. Qwen). It drives the desktop WeChat 4.x client through UI automation — no hooking, injection or web protocol — reads the room before replying, opens topics on her own, keeps long-term memory, sends stickers, @mentions people for real, draws pictures, and can hand tasks to Claude Code or Codex.

Supports private chats, group chats, editable long-term memory, and stickers. The control panel starts **paused**: choose a chat and start it yourself. This is an experimental project; WeChat updates, window layout, and display scaling can affect recognition.

## Features

- **Conversational participation:** batches consecutive messages and checks whether a reply is appropriate. Group participation considers mentions, recent context, and speaking frequency.
- **Follow-ups and opening topics:** can continue private conversations after a delay, bubble up in a group that has gone quiet, and message the designated contact every few hours. When a chat is due the model decides whether anything is worth saying; quiet hours apply and she stops after openings nobody answers.
- **Editable personas:** includes Xiaoqian, a cheerful and mildly sarcastic companion, and Awen, a gentler persona. Create and edit your own in the control panel.
- **Long-term memory:** stores people, group slang, and conversation style separately for each chat. Progress survives restarts, and quiet chats are brought up to date periodically even when they are not open. Manual edits include backups and conflict detection.
- **Images and stickers:** vision models can describe images and select stickers by their descriptions. Collected stickers enter a review queue.
- **Web search:** DuckDuckGo HTML search with Bing as a fallback.
- **Multiple chats:** monitors only allowlisted conversations and supports relays from a designated contact's private chat to other allowlisted chats.
- **Owner sends:** in the designated contact's private chat, "给某某发：…" or "在某群里说：…" sends the text after the colon (whitespace collapsed) to any WeChat chat found by name (short group names are matched only when unambiguous), without the model, and reports back whether it was really sent. @mentions are picked from WeChat's member list like a person would; free wording is read by the local model and then checked.
- **Drawing:** "画一只橘猫" / "帮我画：…" gets a picture from Codex's image tool, sent back to the chat that asked; asked to follow an online look, fan art or a character, it searches for references first. Drawing rights and daily limits are set separately for each allowed group or private chat; the owner has no limit. The console's 画图 tab shows live job states and manages past pictures.
- **Asked means done:** real work the owner mentions in passing (a drawing, research to summarize, a file to make) is handed to the right agent, and she replies with the real receipt instead of promising.
- **Blocking and memory on command:** "拉黑 X" / "解除拉黑 X" / "黑名单" and "总结记忆", in the private chat or by @-ing her in a group.
- **Local agent tasks (on by default, can be turned off):** the designated contact's private chat can hand a task to a local command-line agent (Claude Code, Codex or DeepSeek Harness) with an optional passphrase, and gets a one-line report when it finishes. See [docs/USAGE.md](docs/USAGE.md).

## Requirements

- Windows 10 or 11, with **64-bit Python 3.11** and Tkinter.
- A logged-in desktop WeChat client. The adapter targets **WeChat 4.1.x** and has been tried with **4.1.15.13**. Other versions may require calibration.
- LM Studio, its `lms` CLI, and a downloaded model. Image understanding and sticker labeling require a vision-capable model.
- The Windows Chinese OCR component for group nickname recognition.
- Enough GPU memory for your chosen model. The loader requests full GPU offload; this does not guarantee that every configuration avoids system memory pressure.

The current interface and built-in prompts are in Chinese. This English README documents installation and operation; it does not add an English interface.

## Installation

Run these commands from the project directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

After installing dependencies, you can also double-click `启动小千.cmd`. The application creates `data/` locally on first run. The repository includes no chat history, learned memories, personal stickers, account configuration, or model weights.

## First run

1. Start LM Studio's local API server. The default endpoint is `http://127.0.0.1:1234/v1`.
2. In **Models** (`模型`), select a model you have downloaded. The default model key is a placeholder. The `lms` path is derived from your Windows user directory; update `data/settings.json` if needed.
3. In **Chats** (`聊天`), refresh the conversation list, add a test conversation, and select it. Remove unused placeholder entries.
4. Set the bot's WeChat nickname, designated contact, and persona. For groups, run the read-only recognition calibration and check the detected names first.
5. Keep WeChat visible and click **Start / Continue** (`开始 / 继续`). Verify behavior in a test private chat before enabling groups.

If the window is obscured or minimized, or a send cannot be confirmed, the application stops that operation and reports its status. Check WeChat before continuing.

## Editing memory

Open **Memory → Edit Memory** (`记忆 → 编辑记忆`). Add, edit, or remove people and slang entries, or change the chat's conversation style. Click **Save Changes** (`保存修改`); the next reply uses the updated memory.

- Previous versions are backed up under `data/memory/backups/`.
- If memory changes while you are editing, your draft is kept and a conflict is reported instead of overwriting the newer version.
- Memory continues to evolve with later conversations. Manual edits do not permanently lock facts; deleted information may be learned again from new messages.

## Images and stickers

Put your own PNG, JPEG, GIF, or WebP files in `data/stickers/`, then organize them in **Stickers** (`表情包`). Review and edit descriptions to explain when each sticker is appropriate. Analyzing many images increases GPU load.

Stickers collected from the chat interface are screenshots, so animated stickers become static images. This project does **not** include a tool for decrypting and exporting WeChat's saved-sticker database.

Sending images temporarily changes the clipboard. The application attempts to restore its previous text; it cannot fully restore a previous image clipboard.

## Data and privacy

Runtime configuration, chat context, memory, screenshots, stickers, logs, and backups are stored locally under `data/`, which is excluded by `.gitignore`.

- Model requests go to the configured API endpoint. The default is local; choosing a remote endpoint sends the supplied conversation, images, and memory to that service.
- Web search sends queries to DuckDuckGo or Bing. Disable it in **Pacing** (`节奏`) if unwanted.
- Messages and stickers are sent through WeChat to configured conversations.
- LM Studio manages its own logging and data retention separately.

Memory filtering is heuristic and cannot guarantee removal of every sensitive detail or incorrect inference. Review learned memories regularly. Clearing current memory does not delete its backups or other logs.

See [Data and Privacy](docs/PRIVACY.md), [Usage Guide](docs/USAGE.md), and [Third-party Dependencies and References](THIRD_PARTY.md). Those detailed documents are currently in Chinese.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest test_core test_runner test_memory_edit test_websearch test_routing test_agents test_agent_bridge test_proactive -v
```

Default tests use temporary data, simulated WeChat adapters, and mocked model responses. They do not send WeChat messages. The live search smoke test runs only with `LIVE_SEARCH=1`; OCR tests require the appropriate Windows language component. Automated tests do not replace manual verification against your WeChat version.

## Project layout

| File | Purpose |
| --- | --- |
| `app.py` | Control panel, worker thread, and chat scheduling |
| `runner.py` | New-message tracking, reply timing, and send control |
| `brain.py` | Model requests, participation decisions, image descriptions, and memory extraction |
| `wechat_adapter.py` | Desktop WeChat interface adapter |
| `memory.py` / `memory_editor.py` | Memory storage, validation, and editing forms |
| `stickers.py` | Sticker descriptions, selection, and review queue |
| `websearch.py` | Standard-library search implementation |
| `ocr.py` / `senders.py` | Chinese OCR, nickname correction, and avatar matching |
| `settings.py` / `personas.py` | Local settings and personas |
| `lmstudio.py` | LM Studio model loading and instance management |
| `inspect_group.py` | Read-only group recognition calibration |
| `proactive.py` | Pacing, quiet hours and unanswered limits for opening topics |
| `agents.py` / `routing.py` | Local agent tasks; multi-chat scheduling and relay recipients |
| `data/` | Personal runtime data; never commit this directory |

## Known limitations

This application relies on a visible, stable desktop interface. Group nickname OCR, message direction, quoted messages, and media recognition can be wrong. Rapid chat switching or large message bursts may cause uncertain replies to be skipped. Search providers can change their HTML or restrict access. Model responses and learned memories can contain mistakes.

The model loader switches only the bot's own model instance. The working-set trimming helper can affect other `llama-server` processes on the same machine; it is a memory-management aid, not a guarantee of lower resource usage.

Let conversation participants know they are interacting with an AI. Before publishing your own changes, review staged files and exclude personal data, screenshots, logs, credentials, and machine-specific configuration.

## Version history

Every update is tagged as a small version (`v0.x`); the full notes are in [CHANGELOG.md](CHANGELOG.md).

- **v0.7** — Drawing rights and limits per allowed chat, live drawing-job states, a chat selector that stays synchronized, and periodic memory updates for every allowed chat with progress restored after restart.
- **v0.6** — Real work mentioned in the owner's chat is handed to an agent (drawings to the draw agent, the rest to Codex) instead of being promised; drawings can look up references online first; a 画图 console tab shows where pictures are, previews, opens and deletes them.
- **v0.5** — Drawing: the owner or group members ask, Codex's image tool draws, the picture goes back to that chat; members get 3 a day, the owner has no limit.
- **v0.4** — Opening topics on her own: a bubble in a group that has gone quiet, a message to the designated contact every few hours. The model decides whether to speak; quiet hours and unanswered limits apply, and the pacing survives restarts.
- **v0.3** — Real @mentions picked from the member list; free-worded send requests read by the local model and checked; blocking and a block list; "总结记忆" on request, and memory updates that no longer fail on long-thinking models; stickers before small emoji, no repeats.
- **v0.2** — Hand tasks to local agents (Claude Code / Codex / DeepSeek Harness) from the designated contact's chat; "给某某发：…" sends to any chat and reports truthfully; multi-chat relays.
- **v0.1** — First sanitized release: personas, long-term memory, stickers, web search, multi-chat monitoring and the console.

## Contributors

- [AnthonyZZH](https://github.com/AnthonyZZH) — maintainer
- [Codex](https://github.com/codex) (OpenAI) — implementation, review and testing
- Claude (Anthropic) — implementation and review
