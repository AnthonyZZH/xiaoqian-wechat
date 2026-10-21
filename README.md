# Xiaoqian · AI WeChat Companion

English | [简体中文](README.zh-CN.md)

A Windows desktop companion for WeChat, powered by local models through LM Studio. It reads and sends messages through the WeChat interface, decides when to join a conversation, and replies using a configurable persona.

Supports private chats, group chats, editable long-term memory, and stickers. The control panel starts **paused**: choose a chat and start it yourself. This is an experimental project; WeChat updates, window layout, and display scaling can affect recognition.

## Features

- **Conversational participation:** batches consecutive messages and checks whether a reply is appropriate. Group participation considers mentions, recent context, and speaking frequency.
- **Follow-up messages:** can continue private conversations after a delay. Group participation responds to incoming messages rather than opening topics on a timer.
- **Editable personas:** includes Xiaoqian, a cheerful and mildly sarcastic companion, and Awen, a gentler persona. Create and edit your own in the control panel.
- **Long-term memory:** stores people, group slang, and conversation style separately for each chat. Manual edits include backups and conflict detection.
- **Images and stickers:** vision models can describe images and select stickers by their descriptions. Collected stickers enter a review queue.
- **Web search:** DuckDuckGo HTML search with Bing as a fallback.
- **Multiple chats:** monitors only allowlisted conversations and supports relays from a designated contact's private chat to an allowlisted group.

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
.\.venv\Scripts\python.exe -m unittest test_core test_runner test_memory_edit test_websearch -v
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
| `data/` | Personal runtime data; never commit this directory |

## Known limitations

This application relies on a visible, stable desktop interface. Group nickname OCR, message direction, quoted messages, and media recognition can be wrong. Rapid chat switching or large message bursts may cause uncertain replies to be skipped. Search providers can change their HTML or restrict access. Model responses and learned memories can contain mistakes.

The model loader switches only the bot's own model instance. The working-set trimming helper can affect other `llama-server` processes on the same machine; it is a memory-management aid, not a guarantee of lower resource usage.

Let conversation participants know they are interacting with an AI. Before publishing your own changes, review staged files and exclude personal data, screenshots, logs, credentials, and machine-specific configuration.
