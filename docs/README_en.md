# 📚 MOMOKA Detailed Documentation (English)

> **If you only want to use the hosted bots**, start at the [website](https://momoka-project.com/) ([FAQ](https://momoka-project.com/faq.html) / [Troubleshooting](https://momoka-project.com/troubleshooting.html)).  
> This document is mainly for **self-hosting and developers**.  
> Listing copy: [App Directory](bot_listing_discord.md) · [top.gg](bot_listing_topgg.md) ([index](bot_listing.md))

## Table of Contents

- [Overview](#overview)
- [Main Features](#main-features)
- [Setup](#setup)
- [Configuration](#configuration)
- [Command List](#command-list)
- [Feature Details](#feature-details)
- [Troubleshooting](#troubleshooting)

---

## Overview

**MOMOKA** runs **two Discord bots in one process**: **PLANA** (primary) and **ARONA** (companion). Each bot works standalone for LLM chat and music. Debate and cross-check require both bots in the same guild.

### Dual Bot

| Bot | Role | Invite |
|-----|------|--------|
| **PLANA** | Primary — LLM, music, TTS, images, notifications, trackers, Link Fix, utilities | [Invite](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot) |
| **ARONA** | Companion — LLM, music, utilities. TTS/images/notifications/trackers/Link Fix redirect to PLANA | [Invite](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=6516795221339600&scope=bot) |

- Create **two Discord Applications** and enable **Message Content Intent** on both
- The old root `config.yaml` / `config.default.yaml` are **not used** (no compatibility)

### Features

- 🤖 **Multi-model AI chat** — OpenAI, Google Gemini, NVIDIA NIM, KoboldCPP, and more
- 🗣️ **debate / cross_check** — Multi-round PLANA↔ARONA debate with a judge turn, or a light 3-step verification
- 🎵 **Music playback** — YouTube, Spotify, Google Drive, and more (both bots)
- 🎨 **Image generation / TTS / notifications / trackers** — **PLANA only**
- 🔗 **Link Fix** — Suppress original social embeds and quote-replace via fixer proxies (`/linkfix`, **PLANA only**)
- 🎲 **Utilities** — `/help` (🇯🇵/🇺🇸 + paging; app→guild→en) and `/invite` (Components V2), timers, media download (`/download_video` / `/download_audio`, Components V2), and more

---

## Main Features

### 1. AI Chat (LLM)

Mention `@PLANA` or `@ARONA` to chat.

#### Supported Models

- **OpenAI**: GPT-4o, GPT-4 Turbo
- **Google**: Gemini family
- **NVIDIA NIM**: Kimi, Llama, DeepSeek R1, and more
- **KoboldCPP**: Local LLM servers

#### Highlights

- Image recognition (supported models)
- Conversation history
- Web search
- **debate** — Multi-round PLANA↔ARONA discussion, then a judge turn
- **cross_check** — PLANA draft → ARONA review → PLANA conclusion (3 posts, no stop panel)
- **Automatic API key rotation** on rate limits / server errors

### 2. Music Playback

Both bots can play in voice channels (queue, loop, shuffle, etc.).

#### Sources

- YouTube / Spotify / Google Drive / NicoNico / other yt-dlp media

### 3. Image Generation (PLANA only)

Built-in **diffusers** engine (no external service required). Models under `models/image-models/`. Optional Stable Diffusion WebUI Forge API.

Requests on ARONA are redirected to PLANA.

### 4. Text-to-Speech (PLANA only)

Integrated [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2). Models under `models/tts-models/`. See `NOTICE`.

### 5. Game Trackers (PLANA only)

Rainbow Six Siege / VALORANT stats.

### 6. Notifications (PLANA only)

Earthquake alerts and Twitch stream notifications.

### 7. Link Fix (PLANA only)

When a supported SNS URL is posted, PLANA **suppresses the original Discord embed** and silently quote-replies with a fixer-proxy URL so media previews work.

- Examples: X (Twitter), Instagram, TikTok, Reddit, Threads, Bluesky, Facebook, Pixiv, YouTube, and more
- Skip with `fxignore` in the message, or wrap the URL in `<>`
- For X posts, if the guild `preferred_locale` is known, `🌐` / flag buttons switch original vs translated embeds (FxEmbed-compatible fixers only)
- `/linkfix` (Manage Server): per-guild master/site toggles, **bulk enable/disable all sites**, and fixer source/destination domains (Components V2; **disabled by default** — enable when needed)
- Config: `configs/link_fix_config.yaml`; guild overrides in `data/link_fix_settings.json`
- If the fixer reply gets no embed, it is deleted and the original embed suppress is undone
- Suppressing the original embed requires **Manage Messages**

### 8. Utilities

Dice, timers, server/user info, gacha, etc. `/help` uses Components V2 with **🇯🇵/🇺🇸 language toggle and paging** (LLM / Music+Download / Link Fix / Twitch highlighted first). `/invite` also uses Components V2 for both bot invites.

**UI language (Components V2 / Modal):** Resolved as Discord client locale (app) → guild `preferred_locale` → English (music Now Playing panels are excluded). Covers `/help`, `/invite`, `/linkfix`, LLM waiting/debate panels, media download, `/feedback` Modal, `/match_time` Modal, image-generation Modal, and related surfaces.

**Slash command descriptions:** Shown according to the Discord client language (Japanese / English / Korean / Vietnamese / Chinese Simplified & Traditional / Spanish / French / German / Portuguese / Russian / Thai / Indonesian) via `configs/commands_i18n_config.default.yaml` (loaded directly; no runtime copy). Missing translations and unsupported locales fall back to English. Command names stay in English.

#### Media download (Components V2)

Fetches media with yt-dlp and shares it via Google Drive (links expire after a delay).

| Command | Description |
|---------|-------------|
| `/download_video <query>` | Download from a URL or search query. Shows a Components V2 format picker, then auto-merges best audio and shares |
| `/download_audio <query> <format>` | Extract audio only (mp3 / m4a / opus / flac / wav) and share |

- Format labels do **not** show “video only” notes (audio is merged after selection)
- Requires Google Drive API `client_secrets.json` / `token.json` and the folder ID configured in the cog
- For YouTube, Deno (recommended) or Node.js 22+ plus `yt-dlp[default]` is advised (same EJS guidance as music)

---

## Setup

### Requirements

- **Python 3.11.x** (required; 3.10 / 3.12+ not supported)
- Discord bot tokens for PLANA and ARONA
- **Message Content Intent** enabled on both applications
- API keys as needed
- (Optional) Netscape-format `youtube_cookie.txt` in the project root

### Installation

1. **Clone**
   ```bash
   git clone https://github.com/coffin399/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **Configuration**
   - On first run, each missing `configs/<category>_config.yaml` is copied from the matching `*_config.default.yaml` (except `commands_i18n`, which is loaded directly from the default file)
   - Manual copy example:
     ```bash
     copy configs\bots_config.default.yaml configs\bots_config.yaml   # Windows
     cp configs/bots_config.default.yaml configs/bots_config.yaml     # Linux/Mac
     ```
   - **Root `config.yaml` is not read**

3. **Required edits**
   - `configs/bots_config.yaml` — `bots.plana.token` and `bots.arona.token`
   - `configs/llm_config.yaml` — API keys

4. **Invite bots**
   - [PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot)
   - [ARONA](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=6516795221339600&scope=bot)
   - Invite **both** to any guild where you want debate / cross_check

5. **Start**

   **Windows (recommended):**
   ```bash
   startMOMOKA.bat
   ```

   **Manual:**
   ```bash
   py -3.11 -m venv .venv
   .venv\Scripts\activate  # Windows / source .venv/bin/activate  # Linux/Mac
   pip install -r requirements.txt
   python main.py
   ```
   On start, the GUI log panes are General / LLM / TTS+Music / Error. Music logs go to TTS+Music.  
   The log viewer lives under `MOMOKA/GUI/`. Version constants are in `MOMOKA/version.py` (Discord status date = last git commit).

---

## Configuration

Settings live under `configs/` as category YAML files. See each `*_config.default.yaml` for full keys.

| File | Purpose |
|------|---------|
| `bots_config.yaml` | PLANA/ARONA tokens, invites, roles |
| `llm_config.yaml` | Models, provider API keys, personas |
| `music_config.yaml` | Volume, queue, cookie path, etc. |
| `tts_config.yaml` | TTS models (PLANA) |
| `images_config.yaml` | Image generation (PLANA) |
| `notifications_config.yaml` | Earthquake / Twitch (PLANA) |
| `tracker_config.yaml` | Game stats (PLANA) |
| `debate_config.yaml` | debate / cross_check |
| `link_fix_config.yaml` | Link Fix (suppress + quote-replace social embeds, PLANA) |
| `count_config.yaml` | Server-count posting to bot lists (top.gg, Void Bots, DEL, etc., PLANA) |
| `utilities_config.yaml` | Utilities |
| `core_config.yaml` | Shared core settings |
| `commands_i18n_config.default.yaml` | Slash command description i18n catalog (loaded directly; no runtime copy) |

#### Bot tokens (`bots_config.yaml`)

```yaml
bots:
  plana:
    token: YOUR_PLANA_BOT_TOKEN
  arona:
    token: YOUR_ARONA_BOT_TOKEN
```

#### LLM (`llm_config.yaml`)

```yaml
llm:
  model: "google/gemini-2.5-pro"
  providers:
    google:
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2  # rotates on rate limit
```

#### Images / TTS / Music

- Images: place models at `models/image-models/<name>/<name>.safetensors`; configure via images/LLM image settings (`local` or `forge`)
- TTS: `models/tts-models/` + `tts_config.yaml`
- Music: `music_config.yaml` (`default_volume`, `max_queue_size`, cookie path, etc.)

---

## Command List

### AI Chat (LLM)

| Command | Description |
|---------|-------------|
| `@PLANA` / `@ARONA` `<message>` | Chat via mention |
| `/chat <message>` | Chat without mention |
| `/clear_history` | Reset history |
| `/switch-models` | Per-channel model |

\* `debate` / `cross_check` / `feedback` are LLM tools (multi-round debate + judge / light 3-step check / developer feedback form).
\* `max_tool_iterations` (default 5) caps tool round-trips; if exceeded, the bot generates a final answer from existing tool results without further tools.

### Music

| Command | Description |
|---------|-------------|
| `/play` `/pause` `/resume` `/stop` `/skip` | Playback |
| `/seek` `/volume` `/queue` `/shuffle` `/clear` `/remove` `/nowplaying` `/loop` | Queue & volume |

Now Playing panel (Components V2): title (##) with channel under it; progress as one inline-code line (`bar time / total`). Pause / Skip / Stop (Confirm/Cancel) / Loop / QLoop. Queue list (up to 5 + paging) only when upcoming tracks exist. URL `/play` queries are kept as history on the stopped panel.
Playlist fetch limit is `music.max_playlist_items` (default 10000).
Music messages are sent `@silent` (suppress notifications) by default.

### Image Generation (PLANA)

| Command | Description |
|---------|-------------|
| Ask `@PLANA` to generate an image | Via AI tools |

### TTS (PLANA)

| Command | Description |
|---------|-------------|
| `/say <text>` | Speak text |
| `/tts-help` | TTS help |

### Game Trackers (PLANA)

| Command | Description |
|---------|-------------|
| `/r6s` / `/valorant` | Player stats |

### Notifications (PLANA)

| Command | Description |
|---------|-------------|
| `/earthquake_*` | Earthquake alerts |
| `/twitch_add` `/twitch_remove` `/twitch_list` | Twitch alerts |

### Link Fix (PLANA)

| Command / action | Description |
|------------------|-------------|
| Post an SNS URL | Suppress the original embed, then silent quote-reply with a fixer URL |
| `/linkfix` | Master/site/bulk-all toggles and fixer source/destination (Components V2, Manage Server; disabled by default) |
| Include `fxignore` | Skip Link Fix for that message |

### Utilities

| Command | Description |
|---------|-------------|
| `/help` | Help (Components V2; app→guild→en initial language; 🇯🇵/🇺🇸 toggle + paging) |
| `/invite` | PLANA / ARONA invites (Components V2; app→guild→en) |
| `/download_video` `/download_audio` | Media download (Components V2, Google Drive share) |
| `/ping` `/serverinfo` `/userinfo` `/avatar` | Info |
| `/roll` `/diceroll` `/check` `/gacha` `/timer` `/meow` `/support` `/feedback` | Misc |

---

## Feature Details

### debate / cross_check

- **debate**: Alternating PLANA↔ARONA turns, then a judge turn with summary and recommendation. Both bots must be in the guild
- **cross_check**: PLANA draft → ARONA review → PLANA conclusion; all three steps are posted. Lighter than debate; no stop panel
- Each message is prefixed with a mention of the partner bot

### API key rotation

Set `api_key1`, `api_key2`, … per provider in `llm_config.yaml`. On rate limit / server error, the next key is tried automatically.

### Music

Up to 10,000 queued tracks, loop modes, volume 0–200%, auto-leave when the VC is empty.

### Image generation (PLANA)

Place models under `models/image-models/`, use `provider: "local"` (default). For Forge, start with `--api` and set `provider: "forge"`.

### Earthquake alerts (PLANA)

Real-time JMA WebSocket alerts (early warning, quake info, tsunami forecast).

### Link Fix (PLANA)

- On a matched URL, waits briefly for Discord’s official embed, **suppresses it**, then quote-replies with a fixer URL
- Replies are `@silent`; if the fixer reply gets no embed, it is deleted and the original suppress is undone
- `/linkfix` lets each guild pick fixer denominations (e.g. `fxtwitter.com` / `vxtwitter.com`) and match domains; also supports bulk enable/disable all sites (disabled by default — enable when needed)
- Suppressing the original embed requires Manage Messages

---

## Troubleshooting

### Bot won't start

1. Check `bots.plana.token` / `bots.arona.token` in `configs/bots_config.yaml`
2. Confirm Python 3.11.x
3. Confirm dependencies are installed
4. A root `config.yaml` alone will not work — use `configs/`

### AI doesn't respond

1. Check API keys in `configs/llm_config.yaml`
2. Enable **Message Content Intent** on both applications
3. Confirm the selected model is available

### debate / cross_check fail

1. Ensure both PLANA and ARONA are in the same server
2. Use `/invite` to add the missing bot

### Music won't play

1. Check VC connection, FFmpeg, and `youtube_cookie.txt`
2. **YouTube EJS**: install Deno (recommended) or Node.js 22+ on PATH; run `pip install -U "yt-dlp[default]"`

### Image generation fails

1. Use PLANA (ARONA only redirects)
2. Check model paths and images/LLM image settings

### Earthquake alerts missing

1. `/earthquake_status` on PLANA
2. Check notification channel and WebSocket connection

---

## Support

- Discord: [https://discord.com/invite/H79HKKqx3s](https://discord.com/invite/H79HKKqx3s)
- `/support` command
- `/feedback` — submit bug reports / feature requests via Modal to developer channels (also available from LLM chat)

### Feedback setup (self-host)

Set one or more destination channel IDs under `feedback.channel_ids` in `configs/utilities_config.yaml` (the bot must be in that guild). If empty, `/feedback` and the LLM `feedback` tool cannot deliver reports.

### License

- This project: **AGPL-3.0**
- Style-Bert-VITS2 integration: AGPL-3.0 / LGPL-3.0 (see `NOTICE`)

---

**Made by [coffin299](https://discord.com/users/270446628622696449) & [zer0latency](https://discord.com/users/583206903442571264)**

&copy; 2026 MOMOKA
