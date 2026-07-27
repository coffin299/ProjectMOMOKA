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

> **JA:** 日英で差異がある場合は **日本語を優先**します。  
> **EN:** If Japanese and English differ, **Japanese takes priority**.

---

## まず使う / Get started (recommended)

**JA:** 公開ホストのボットを招待するだけで使えます。セットアップ不要です。  
**EN:** Just invite the hosted bots — no setup required.

1. **[Invite PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160)**  
   **JA:** フル機能 · **EN:** full feature set
2. **ARONA（任意）** — **JA:** PLANA の `/help` または `/invite` から追加 · **EN:** add via PLANA `/help` or `/invite`
3. **[Website](https://momoka-project.com/)** — [FAQ](https://momoka-project.com/faq.html) · [Troubleshooting](https://momoka-project.com/troubleshooting.html)

**JA:** サポート — [Discord](https://discord.gg/H79HKKqx3s) · DM [coffin299](https://discord.com/users/270446628622696449)  
**EN:** Support — [Discord](https://discord.gg/H79HKKqx3s) · DM [coffin299](https://discord.com/users/270446628622696449)

---

## What is MOMOKA? / MOMOKA とは

**JA:** **MOMOKA** はプロジェクト名です。実際に動く Discord ボットは **PLANA**（プライマリ）と **ARONA**（コンパニオン）の2体で、1プロセスで両方を動かします。  
**EN:** **MOMOKA** is the project name. The Discord bots are **PLANA** (primary) and **ARONA** (companion), run together in one process.

| Bot | JA | EN |
|-----|----|----|
| **PLANA** | LLM・音楽・TTS・画像・通知・tracker・Link Fix・メディアDL・utilities | LLM, music, TTS, images, notifications, trackers, Link Fix, media download, utilities |
| **ARONA** | LLM・音楽・slash。TTS/画像/通知/tracker/Link Fix は PLANA へ誘導（`/help`・`/invite` から任意追加） | LLM, music, slash. TTS / images / notifications / trackers / Link Fix redirect to PLANA (optional via `/help` / `/invite`) |

> **Built-ins（セルフホスト時 / when self-hosting）**
> - Image generation — `MOMOKA/generator/image` (diffusers) / `image_generator_config`
> - Style-Bert-VITS2 TTS — `third_party/style_bert_vits2` + wrapper `MOMOKA/generator/tts` (see `NOTICE`)
> - Log viewer GUI — `MOMOKA/GUI/` (version: `MOMOKA/version.py`; Discord status date = last git commit; `[USER_INPUT]` yellow-green / `[LLM_RESPONSE]` cyan; `[PLANA]` / `[ARONA]` tags unchanged)

### Key Features / 主な機能

- **AI Chat (LLM)** — `@PLANA` / `@ARONA`, `/chat`, **Agent router** (conversation / coding / command), web search, image understanding
- **Music** — YouTube / Spotify, etc. (**JA:** 両ボット · **EN:** both bots)
- **TTS / images / notifications / trackers** — **JA:** PLANA のみ · **EN:** PLANA only
- **Link Fix** — **JA:** 公式 SNS embed を抑制し Fix URL で引用置換（`/linkfix` で全体・サイト別・一括 on/off、デフォルト無効・必要時に有効化、PLANA のみ） · **EN:** suppress original social embeds and quote-replace via fixers (`/linkfix` master/site/bulk toggles, disabled by default — enable when needed, PLANA only)
- **Utilities** — `/help` (🇯🇵/🇺🇸 + paging; app→guild→en) `/invite` `/support` `/feedback`, `/download_video` `/download_audio`, …

**JA:** Components V2 / Modal の表示言語は **クライアント言語 → サーバー言語 → English**（音楽 UI 除く）。スラッシュコマンドの説明文は Discord クライアント言語（日本語 / 英語 / 韓国語 / ベトナム語 / 中国語簡体・繁体 / スペイン語 / フランス語 / ドイツ語 / ポルトガル語 / ロシア語 / タイ語 / インドネシア語）に応じて表示し、欠落時は英語。最新コマンドは `/help` または [コマンド一覧](https://momoka-project.com/commands.html)  
**EN:** Components V2 / Modal UI language is **app locale → guild locale → English** (music UI excluded). Slash command descriptions follow the Discord client language (Japanese / English / Korean / Vietnamese / Chinese Simplified & Traditional / Spanish / French / German / Portuguese / Russian / Thai / Indonesian), falling back to English. Prefer `/help` in Discord, or the [command list](https://momoka-project.com/commands.html)

---

## Self-host / セルフホスト（上級者向け / advanced）

**JA:** 依存が多く重いです。一般利用は上記の招待を推奨します。  
**EN:** Heavy dependencies. For normal use, invite the hosted bots above.

### Requirements / 要件

- **Python 3.11.x** (**JA:** 必須 · **EN:** required)
- **Node.js 20+** (**JA:** 同梱PO Token Providerに必須 · **EN:** required by bundled PO Token Provider)
- Discord Application ×2 (PLANA + ARONA), both with **Message Content Intent**
- Optional: Netscape-format `youtube_cookies.txt` in the project root

### Configure / 設定

**JA:** ルートの `config.yaml` は **使いません**。`configs/` 配下のみです。  
**EN:** Root `config.yaml` is **not used**. Use `configs/` only.

1. First run copies `configs/<category>_config.default.yaml` → `configs/<category>_config.yaml` (or copy manually; `commands_i18n` loads the default file directly)
2. Set tokens / invite URLs in `configs/bots_config.yaml`
3. Set API keys in `configs/llm_config.yaml`
4. Details: [docs/README_ja.md](docs/README_ja.md) · [docs/README_en.md](docs/README_en.md)

### Run / 起動

```bash
git clone https://github.com/coffin299/ProjectMOMOKA.git
cd ProjectMOMOKA
startMOMOKA.bat          # Windows (recommended)
# or: python main.py     # after installing deps
```

`startMOMOKA.bat` builds the bundled BgUtils Provider v1.3.1 once. MOMOKA
starts it before both bots, checks `http://127.0.0.1:4416/ping`, and stops
the owned process during shutdown. Provider/yt-dlp output is redacted and
shown in the GUI **TTS+Music** log. Block inbound TCP 4416 in Windows
Firewall because upstream v1.3.1 may bind to non-loopback interfaces.

---

## Docs & Legal / ドキュメント・法務

| | |
|---|---|
| Website | https://momoka-project.com/ |
| FAQ / Troubleshooting | https://momoka-project.com/faq.html · https://momoka-project.com/troubleshooting.html |
| Terms / Privacy | https://momoka-project.com/terms.html · https://momoka-project.com/privacy.html |
| Detailed setup | [🇯🇵](docs/README_ja.md) · [🇺🇸](docs/README_en.md) |
| Bot listing copy | [index](docs/bot_listing.md) · [App Directory](docs/bot_listing_discord.md) · [top.gg](docs/bot_listing_topgg.md) |
| License | **AGPL-3.0** — Style-Bert-VITS2: AGPL/LGPL (`NOTICE`) |

---

<div align="center">

**MOMOKA** · [momoka-project.com](https://momoka-project.com/)

&copy; 2026 MOMOKA · [coffin299](https://discord.com/users/270446628622696449) &amp; [zer0latency](https://discord.com/users/583206903442571264)

</div>
