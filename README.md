<div align="center">

![Moe Counter](https://count.getloli.com/@prjMOMOKAGitHub?name=prjMOMOKAGitHub&theme=green&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=0)

# MOMOKA

**JA:** 多機能 Discord ボット — PLANA（主）+ ARONA（コンパニオン）。AIチャット・音楽・読み上げ・通知・Link Fix など。  
**EN:** Multi-functional Discord bot — PLANA (primary) + ARONA (companion). AI chat, music, TTS, notifications, Link Fix, and more.

[![Website](https://img.shields.io/badge/Website-momoka--project.com-1c1917?style=for-the-badge)](https://momoka-project.com/)
[![Invite PLANA](https://img.shields.io/badge/Invite%20PLANA-24/7%20Online-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/oauth2/authorize?client_id=1031673203774464160)

</div>

<div align="center">

![Python](https://img.shields.io/badge/python-3.11-blue.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.7+-blue.svg)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/coffin299/ProjectMOMOKA)
[![Discord](https://img.shields.io/discord/1305004687921250436?logo=discord&logoColor=white&label=Discord&color=5865F2)](https://discord.gg/H79HKKqx3s)
[![Discord Bots](https://top.gg/api/widget/servers/1031673203774464160.svg)](https://top.gg/bot/1031673203774464160)
[![Discord App Directory](https://img.shields.io/badge/Discord-App%20Directory-5865F2?logo=discord&logoColor=white)](https://discord.com/discovery/applications/1031673203774464160)

</div>

<div align="center">

[🇯🇵 日本語詳細](docs/README_ja.md) · [🇺🇸 English docs](docs/README_en.md) · [Website](https://momoka-project.com/) · [FAQ](https://momoka-project.com/faq.html) · [Terms](https://momoka-project.com/terms.html) · [Privacy](https://momoka-project.com/privacy.html)

</div>

---

## まず使う / Get started

**JA:** 公開ホストのボットを招待するだけで使えます。セットアップ不要です。  
**EN:** Just invite the hosted bots — no setup required.

1. **[Invite PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160)**  
   **JA:** フル機能 · **EN:** full feature set
2. **ARONA（任意）** — **JA:** PLANA の `/help` または `/invite` から追加 · **EN:** add via PLANA `/help` or `/invite`
3. サーバーで `/help`、または [コマンド一覧](https://momoka-project.com/commands.html)  
   **EN:** Run `/help` in your server, or see the [command list](https://momoka-project.com/commands.html)

**JA:** サポート — [Discord](https://discord.gg/H79HKKqx3s) · DM [coffin299](https://discord.com/users/270446628622696449) · [FAQ](https://momoka-project.com/faq.html)  
**EN:** Support — [Discord](https://discord.gg/H79HKKqx3s) · DM [coffin299](https://discord.com/users/270446628622696449) · [FAQ](https://momoka-project.com/faq.html)

---

## What is MOMOKA? / MOMOKA とは

**JA:** **MOMOKA** はプロジェクト名です。実際に動く Discord ボットは **PLANA**（プライマリ）と **ARONA**（コンパニオン）の2体です。  
**EN:** **MOMOKA** is the project name. The Discord bots are **PLANA** (primary) and **ARONA** (companion).

| Bot | JA | EN |
|-----|----|----|
| **PLANA** | LLM・音楽・TTS・画像・通知・tracker・Link Fix・メディアDL・utilities | LLM, music, TTS, images, notifications, trackers, Link Fix, media download, utilities |
| **ARONA** | LLM・音楽・slash。TTS/画像/通知/tracker/Link Fix は PLANA へ誘導（`/help`・`/invite` から任意追加） | LLM, music, slash. TTS / images / notifications / trackers / Link Fix redirect to PLANA (optional via `/help` / `/invite`) |

---

## AI Chat (LLM) / AI対話

**JA:** `@PLANA` / `@ARONA` をメンションして会話を始め、リプライで会話を続けられます。  
**EN:** Mention `@PLANA` / `@ARONA` to start a conversation, then keep chatting with replies.

- **JA:** 複数モデル対応（OpenAI、Gemini、NVIDIA NIM、OpenRouter など） · **EN:** Multiple models (OpenAI, Gemini, NVIDIA NIM, OpenRouter, and more)
- **JA:** 会話履歴、Web検索、画像理解（対応モデル） · **EN:** Conversation history, web search, and image understanding (supported models)

![AI Chat](https://momoka-project.com/assets/images/conversation.png)

*JA: メンションで開始、リプライで継続 — マルチモデルでサーバー内に返信します。*  
*EN: Mention to start, then reply to keep going — multi-model replies in your server.*

---

## Music Playback / 音楽再生

**JA:** ボイスチャンネルで再生（キュー・ループ・シャッフルなど）。両ボット対応。  
**EN:** Play audio in voice channels with queue, loop, shuffle, and more. Both bots.

- YouTube / Spotify / Google Drive / ニコニコ動画 / その他 yt-dlp 対応ソース  
  **EN:** YouTube, Spotify, Google Drive, NicoNico, and other yt-dlp sources

![Music Playback](https://momoka-project.com/assets/images/playmusic.png)

*JA: ボイスチャンネルでの音楽再生 — キュー・ループ・シャッフル対応。*  
*EN: Play music in a voice channel — queue, loop, and shuffle supported.*

---

## Media Downloader / メディアダウンローダー

**JA:** 動画・音声を取得し、Google Drive 経由で共有します。  
**EN:** Fetch video or audio and share via Google Drive.

- `/download_video` — **JA:** URL または検索語。フォーマット選択（Components V2）後に共有 · **EN:** URL or search; pick a format (Components V2), then share
- `/download_audio` — **JA:** 音声のみ（mp3 / m4a / opus / flac / wav） · **EN:** audio only (mp3 / m4a / opus / flac / wav)

![Media Downloader](https://momoka-project.com/assets/images/media_downloader.png)

*`/download_video` / `/download_audio` — JA: フォーマット選択後、Google Drive 経由で共有。 / EN: format picker, then share via Google Drive.*

---

## Also included / その他

- **TTS** — Style-Bert-VITS2（**JA:** PLANA のみ · **EN:** PLANA only）
- **Link Fix** — **JA:** SNS 向け公式 embed を抑制し Fix URL で引用置換（`/linkfix`、デフォルト無効） · **EN:** suppress original social embeds and quote-replace via fixers (`/linkfix`, disabled by default)
- **JA:** 地震速報 / Twitch 通知 · **EN:** Earthquake / Twitch notifications
- **JA:** ゲーム統計（VALORANT、Rainbow Six Siege） · **EN:** Game trackers (VALORANT, Rainbow Six Siege)
- **Utilities** — `/help` · `/invite` · `/support` · `/feedback` · タイマー など

---

## Docs & Legal / ドキュメント・法務

| | |
|---|---|
| Website | https://momoka-project.com/ |
| FAQ / Troubleshooting | https://momoka-project.com/faq.html · https://momoka-project.com/troubleshooting.html |
| Terms / Privacy | https://momoka-project.com/terms.html · https://momoka-project.com/privacy.html |
| Detailed setup (self-host) | [🇯🇵](docs/README_ja.md) · [🇺🇸](docs/README_en.md) |
| License | **AGPL-3.0** — Style-Bert-VITS2: AGPL/LGPL (`NOTICE`) |

---

<div align="center">

**MOMOKA** · [momoka-project.com](https://momoka-project.com/)

&copy; 2026 prjMOMOKA · Dev by [coffin299](https://discord.com/users/270446628622696449) &amp; [zer0latency (Autmn134F)](https://discord.com/users/583206903442571264)

</div>
