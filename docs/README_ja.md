# 📚 MOMOKA 詳細ドキュメント（日本語）

> **公開ボットを使うだけなら** [公式サイト](https://momoka-project.com/)（[FAQ](https://momoka-project.com/faq.html) / [Troubleshooting](https://momoka-project.com/troubleshooting.html)）を先に見てください。  
> このドキュメントは主に **セルフホスト・開発者向け** です。  
> 掲載文: [App Directory](bot_listing_discord.md) · [top.gg](bot_listing_topgg.md)（[一覧](bot_listing.md)）

## 目次

- [概要](#概要)
- [主な機能](#主な機能)
- [セットアップ](#セットアップ)
- [設定](#設定)
- [コマンド一覧](#コマンド一覧)
- [機能詳細](#機能詳細)
- [トラブルシューティング](#トラブルシューティング)

---

## 概要

**MOMOKA** は、**PLANA** と **ARONA** の2つの Discord ボットを1プロセスで動かす多機能ボットです。AI対話・音楽は各ボット単体でも利用できます。ARONA は任意のコンパニオンで、招待は PLANA の `/help` / `/invite` から行えます。

### デュアルボット

| Bot | 役割 | 招待 |
|-----|------|------|
| **PLANA** | プライマリ。LLM・音楽・TTS・画像・通知・tracker・Link Fix・ユーティリティ | [招待リンク](https://discord.com/oauth2/authorize?client_id=1031673203774464160) |
| **ARONA** | コンパニオン。LLM・音楽・ユーティリティ。TTS/画像/通知/tracker/Link Fix は PLANA へ誘導 | PLANA の /help / /invite から任意追加 |

- Discord Developer Portal で **2つの Application** を用意し、両方に **Message Content Intent** を有効化してください
- 旧ルートの `config.yaml` / `config.default.yaml` は **使用しません**（互換なし）

### 特徴

- 🤖 **マルチモデルAI対話** - OpenAI、Google Gemini、NVIDIA NIM、OpenRouter、KoboldCPP など
- 🧭 **Agent ルーター** - 会話 / コーディング / コマンドへ自動振り分け
- 🎵 **音楽再生** - YouTube、Spotify、ニコニコ動画 など（両ボット）
- 🎨 **画像生成 / TTS / 通知 / tracker** - **PLANA 専用**
- 🔗 **Link Fix** - 公式 SNS embed を抑制し Fix URL で引用置換（`/linkfix`・**PLANA 専用**）
- 🎲 **ユーティリティ** - `/help`（🇯🇵/🇺🇸・ページング・app→guild→en）・`/invite`（Components V2）・`/updates`（Components V2・5件ページング）、`/ping`・`/latex`、メディアダウンロード（`/download_video` / `/download_audio`・Components V2）など

---

## 主な機能

### 1. AI対話機能 (LLM)

`@PLANA` / `@ARONA` でメンションすると AI が応答します。**DM ではメンション不要**で、平文メッセージに応答し会話履歴も保持します。

#### 対応モデル

- **OpenAI**: GPT-4o, GPT-4 Turbo
- **Google**: Gemini 系
- **NVIDIA NIM**: Kimi、Llama、DeepSeek R1 など
- **OpenRouter**: Free Models Router（`openrouter/free`）など（集約 API）
- **KoboldCPP**: ローカル LLM サーバー

#### 主な機能

- 画像認識（対応モデルの場合）
- 会話履歴
- Web 検索
- **Agent** — メンション・reply・/chat の前段で mode を分類
- **自動APIキーローテーション** — 複数キーでレートリミット時に切り替え

### 2. 音楽再生機能

両ボットでボイスチャンネル再生が可能です（キュー・ループ・シャッフルなど）。
ただし **同一 VC への同時接続は不可**（負荷対策）。片方の Bot が接続中のチャンネルへ、もう一方は入れません。既に同居している場合は **ARONA（companion）のみ切断**し、PLANA を維持します。

PCM ミキサー（`AudioMixer`）は NumPy でフレーム加算・クリップし、音楽と TTS を同一 VC で重ねます。

#### 対応ソース

- YouTube / Spotify / ニコニコ動画 / その他 yt-dlp 対応メディア

### 3. 画像生成機能（PLANA 専用）

**内製 diffusers エンジン**（外部サービス不要）。モデルは `models/image-models/` 配下。オプションで Stable Diffusion WebUI Forge API も利用可。

ARONA に画像生成を頼むと、PLANA への誘導メッセージが返ります。

### 4. 音声読み上げ (TTS)（PLANA 専用）

統合 [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2) エンジン（同梱: `third_party/style_bert_vits2/`、ラッパー: `MOMOKA/generator/tts/`）。モデルは `models/tts-models/`。詳細は `NOTICE` を参照。

### 5. ゲーム統計追跡（PLANA 専用）

Rainbow Six Siege / VALORANT の統計表示。

### 6. 通知機能（PLANA 専用）

地震速報・Twitch 配信通知。

### 7. Link Fix（PLANA 専用）

対象 SNS URL を検知すると、**元メッセージの公式 embed を抑制**し、Fix 系プロキシ URL を **silent 引用返信**で置き換えます（動画プレビュー等が使えるようになります）。

- 対応例: X (Twitter)、Instagram、TikTok、Reddit、Threads、Bluesky、Facebook、Pixiv、YouTube など
- 本文に `fxignore` を含める、または URL を `<>` で囲むとスキップ
- X 投稿はサーバー言語（`preferred_locale`）が取れると、返信下に `🌐` / 国旗ボタンで原文・翻訳を切替（FxEmbed 系の Fix 先のみ）
- `/linkfix`（Manage Server）で全体・サイト別 on/off、**全サイト一括**有効化/無効化、Fix 元／Fix 先（宗派）をギルド単位で変更（Components V2・**デフォルト無効**・必要時に有効化）
- 設定: `configs/link_fix_config.yaml`、ギルド上書きは `data/momoka.db`（namespace: `link_fix_settings`）
- Fix 側に embed が付かない場合は返信を削除し、元 embed の抑制を戻します
- 元 embed 抑制には **Manage Messages** が必要です

### 8. ユーティリティ

ダイス、サーバー/ユーザー情報、ガチャ、`/latex`（matplotlib mathtext で数式PNG）など。`/help` は Components V2 で **🇯🇵/🇺🇸 言語切替とページング**（LLM / Music+Download / Link Fix / Twitch を先頭に案内）。`/invite` も Components V2 で両ボットの招待を案内します。`/updates` は GitHub コミットを全件取得し、Components V2 で 5 件ずつ（先頭/前/次/末尾）ページング表示します。

**UI 言語（Components V2 / Modal）:** Discord クライアント言語（app locale）→ サーバーの `preferred_locale`（guild）→ English の順で決定します（音楽 Now Playing 等は対象外）。`/help` `/invite` `/updates` `/linkfix`、LLM 待機・討論パネル、メディア DL、`/feedback` Modal、画像生成 Modal などが対象です。

**スラッシュコマンドの説明文:** Discord クライアント言語（日本語 / 英語 / 韓国語 / ベトナム語 / 中国語簡体・繁体 / スペイン語 / フランス語 / ドイツ語 / ポルトガル語 / ロシア語 / タイ語 / インドネシア語）に応じて表示します（`configs/commands_i18n_config.default.yaml` を直接読み込み）。翻訳が無い言語や未対応 locale は英語にフォールバックします。コマンド名自体は英語のままです。

#### メディアダウンロード（Components V2）

yt-dlp で取得したメディアを **Cloudflare Named Tunnel** 経由の一時リンクで共有します（約 10 分で失効）。第三者ホストへファイルは上げません。

#### Cloudflare Tunnel（media_share）セットアップ要約

1. Zero Trust で Named Tunnel を作成し、Public Hostname を用意する（例: `downloader.momoka-project.com`）
2. サービス URL: `http://127.0.0.1:8765`（Bot と同じ PC で cloudflared 常駐）
3. `configs/core_config.yaml` の `external_services.media_share` で `public_base_url` / `port` を合わせる
4. Bot 起動後、リンクは `https://downloader.momoka-project.com/d/momoka-file-token-<token>` 形式

推奨: Cloudflare 上で `/d/*` のレート制限。Access（ログイン強制）は付けない（共有リンクが開けなくなる）。

| コマンド | 説明 |
|---------|------|
| `/download_video <query>` | URL または検索語から動画を取得。フォーマット選択 UI（Components V2）表示後、最良音声と自動結合して共有 |
| `/download_audio <query> <format>` | 音声のみ抽出（mp3 / m4a / opus / flac / wav）して共有 |

- 動画フォーマット一覧に「映像のみ」などの注記は出しません（選択後に音声を結合するため）
- フォーマット選択では拡張子を先頭表示し、同一解像度では mp4 を webm より優先します
- 対応外 URL やフォーマット取得不可などのエラーは原因別に案内します（タグ／一覧ページは不可）
- **許可リスト**: `configs/media_downloader_allowlist.default.json`（初回起動で `media_downloader_allowlist.json` にコピー）。成人向け IE・プライベート IP は拒否。`/play` には適用しない
- 共有リンクは Cloudflare Tunnel（expire≈600 秒・トークン URL）。エントリは SettingsDB（`media_share_entries`）へ一時保存し再起動後も有効期限内は配信継続。ローカル一時ファイルは約 600 秒後に削除されます
- YouTube 向けには Deno（推奨）または Node.js 22+ と `yt-dlp[default]` を推奨（音楽機能と同じ EJS 対策）

---

## セットアップ

### 必要な環境

- **Python 3.11.x**（必須。3.10 / 3.12 以降は非対応）
- **Node.js 20 以上**（同梱 BgUtils PO Token Provider のビルド・実行に必須）
- Discord Bot Token（PLANA / ARONA 各1つ）
- 両方の Application で **Message Content Intent** を有効化
- 各種 API キー（利用機能に応じて）
- （任意）`youtube_cookies.txt`（Netscape 形式、プロジェクト直下）

### インストール手順

1. **リポジトリのクローン**
   ```bash
   git clone https://github.com/coffin299/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **設定ファイル**
   - 初回起動時、`configs/<category>_config.yaml` が無いカテゴリだけ `*_config.default.yaml` から自動コピーされます（`commands_i18n` は除く。default を直接読みます）
   - **Public リポジトリ注意**: 実行時の `configs/*_config.yaml`（トークン・API キー）、`configs/media_downloader_allowlist.json`（ランタイムコピー）、`.env`、`data/*.db`、資格情報ファイルは **絶対にコミットしない**（`.gitignore` 済み）。default には `YOUR_*` プレースホルダのみを置く
   - 手動でコピーする場合の例:
     ```bash
     copy configs\bots_config.default.yaml configs\bots_config.yaml   # Windows
     cp configs/bots_config.default.yaml configs/bots_config.yaml     # Linux/Mac
     ```
   - **旧ルート `config.yaml` は読みません**

3. **必須の編集**
   - `configs/bots_config.yaml` — `bots.plana.token` / `bots.arona.token`
   - `configs/llm_config.yaml` — API キー

4. **ボットの招待**
   - [PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160)
   - ARONA は PLANA の /help / /invite から任意追加

5. **起動**

   `startMOMOKA.bat` は初回のみ同梱 Provider v1.3.1へ `npm ci` と
   TypeScriptビルドを行います。同様に `gui-electron` も `dist\index.html` が無ければ
   `npm ci` / `npm run build` し、あればスキップします。
   Bot起動前にProviderを開始して `/ping` を確認し、
   GUI・`/shutdown`・Ctrl+Cによる終了時にはMOMOKAが起動したProviderも停止します。
   Providerとyt-dlpの出力は秘密値を除去してGUIの「TTS+Musicログ」へ表示されます。

   > **セキュリティ:** 上流v1.3.1は外部インターフェースへbindする場合があります。
   > Windows Firewallで受信TCP 4416をローカル用途以外から遮断してください。

   **Windows (推奨):**
   ```bash
   startMOMOKA.bat
   ```

   **手動:**
   ```bash
   py -3.11 -m venv .venv
   .venv\Scripts\activate  # Windows / source .venv/bin/activate  # Linux/Mac
   pip install -r requirements.txt
   python main.py
   ```
   起動時のホスト GUI は **Electron**（Discord ダーク）。左サイドバーで Overview（運用）/ 一般 / LLM / TTS+Music / エラー / **ログ管理** を切替。Overview では Active VC・LLM 入出力要約と平均応答時間・参加サーバー（名前/ID）・join/leave・ping/uptime を表示。各区画は独立スクロール。  
   ログ色: `[USER_INPUT]` は黄緑、`[LLM_RESPONSE]` はシアン。`[GUILD_EVENT]` は青。`[PLANA]` / `[ARONA]` タグ色はそのまま。`[LLM_RESPONSE]` には検索用に `requester_id=<DiscordユーザーID>` を付与。  
   API は `127.0.0.1` のみ・起動時 Bearer トークン必須（**ギルド管理者向け Web ダッシュボードとは別**。ブラウザ公開しない）。  
   全ログは `data/momoka_gui.txt` と `data/momoka_gui.log` へ追記。起動時 GUI は `.log` 末尾 **最大 10000 行** を復元表示（再起動後も履歴維持・Join/Leave 含む）。Clear は画面のみ。  
   **ログ管理**パネル: Discord ユーザー ID でログ・DB（`speech_auto_join_users` / VC 再起動セッション内 `requester_id`）を検索。ログは選択／一括で日時のみ残してマスク（`.log`/`.txt`）、DB は選択／一括で完全削除。API: `GET /privacy/search`・`POST /privacy/logs/mask`・`POST /privacy/db/delete`。  
   初回は `startMOMOKA.bat` が `gui-electron` をビルド（`dist\index.html` があればスキップ）。未ビルドでも Bot は起動継続。  
   本体は `MOMOKA/GUI/` + `gui-electron/`。ステータスの **Servers** は PLANA 単体、**VC / LLM** は PLANA + ARONA 合算。

---

## 設定

設定は `configs/` 配下のカテゴリ別 YAML です。各キーの意味は `*_config.default.yaml` 内の日本語コメントを参照してください（実行時は同名の `*_config.yaml`）。

| ファイル | 内容 |
|---------|------|
| `bots_config.yaml` | PLANA/ARONA の token・invite・role |
| `llm_config.yaml` | モデル・プロバイダ API キー・persona |
| `music_config.yaml` | 音量・キュー・Cookie パスなど |
| `tts_config.yaml` | TTS モデル（PLANA） |
| `images_config.yaml` | 画像生成（PLANA） |
| `notifications_config.yaml` | 地震・Twitch（PLANA） |
| `tracker_config.yaml` | ゲーム統計（PLANA） |
| `debate_config.yaml` | debate / cross_check |
| `link_fix_config.yaml` | Link Fix（SNS embed 抑制＋引用置換・PLANA） |
| `count_config.yaml` | 掲載サイト向けサーバー数投稿（top.gg / Void Bots / DEL 等・PLANA） |
| `utilities_config.yaml` | ユーティリティ |
| `core_config.yaml` | コア共通設定（`presence.activity_type` / `presence.status` / `presence.status_rotation`。文言は `{guild_count}` `{version}` `{build_date}` `{status_version}` を置換） |
| `commands_i18n_config.default.yaml` | スラッシュコマンド説明の多言語カタログ（コピーせず直接読み込み） |

### ランタイムデータ（SQLite）

ギルド／チャンネル単位の上書き設定などは `data/momoka.db` に**正規化テーブル**で保存します。

主なテーブル例: `channel_llm_models`, `link_fix_guilds` / `link_fix_sites`, `tts_channel_settings`, `speech_guild_settings`, `twitch_watch`, `earthquake_guild_config`, `logging_channels`, `vc_playback_sessions` など。版は `schema_meta.version`（現行 3）。

TTS / 読み上げ設定のロードに失敗した場合、Cog unload 時の空データ保存は行いません（DB 全消し防止）。読み上げ設定・辞書のギルド単位更新は `SettingsDB.save_guild()` を優先します。`tts_channel_settings` の全体保存は dict 以外を拒否し、DELETE 前に検証します。

#### ホスト設定とギルド設定の境界

Web ダッシュボードが変更できるのはギルド管理 namespace（地震、Twitch、Link Fix、読み上げ設定、読み上げ辞書）だけです。ダッシュボード実装は `SettingsDB.save_guild()` / `save_guild_async()` を使用し、対象ギルド以外の行を変更してはいけません。

`logging_channels`、`log_viewer_config`、`fileio_deletion_schedule`、`media_share_entries`、`response_times` はホスト専用 namespace であり、ギルド管理画面から読み書きしてはいけません。チャンネル単位の LLM・画像モデル・TTS 設定もギルド管理ダッシュボードの対象外です。

#### 認可マトリクス（Discord / 将来ダッシュボード）

| 設定 | Discord 権限（現行） | ダッシュボード公開時 |
|------|----------------------|----------------------|
| Link Fix / 地震 / Twitch 設定変更 | Manage Guild | Manage Guild（サーバー側で再検証） |
| 読み上げ dictionary / speech | 現状はメンバー可の操作あり | **公開前に Manage Guild へ揃える方針** |
| Bot token / LLM API key / Twitch secret | ホスト YAML のみ | **ブラウザに出さない** |
| logging_channels / shutdown / admin | ボット管理者のみ | ダッシュボード対象外 |

#### ホスト運用 GUI（Electron）と将来ギルドダッシュボード

- ホスト GUI API（`/host-gui/*`, `127.0.0.1`, 起動時 Bearer）は Bot 運用者専用。ギルド設定・OAuth・公開ブラウザ UI とは**別系統**
- Host GUI のログ配信は **Bearer 付き SSE**（`GET /host-gui/api/logs/stream`）と `/logs/history` ポーリング。WebSocket `/logs` は互換用（`bearer.<token>` のみ）。メッセージ認証 WS は使わない
- LLM 画像 URL 取得は `MOMOKA.utilities.url_safety` で SSRF 対策（プライベート IP・リダイレクト再検証）
- 将来のギルド管理者ダッシュボードは Discord OAuth + Manage Guild + `save_guild` のみ。ホスト namespace・shutdown・トークン・ローカルサービスプロキシを載せない

#### 将来 Web ダッシュボード設計メモ（未実装）

- Discord OAuth2 でログインし、サーバー側でギルドメンバーシップと Manage Guild を検証する
- クライアント送信の `guild_id` を信じない（IDOR 対策）。永続化は `save_guild` のみ
- CSRF 対策と CORS の Origin allowlist。Bot token をブラウザへ渡さない
- bgutil / Forge などローカルサービスはギルド管理者へプロキシしない
- アクセスログのシークレット伏せ字は `MOMOKA.services.log_sanitize.sanitize_log_message` を再利用する
- Link Fix のカスタム domain は内部ホスト・プライベート IP を拒否する（`normalize_domain`）

#### ボット設定例（`bots_config.yaml`）

```yaml
bots:
  plana:
    token: YOUR_PLANA_BOT_TOKEN
  arona:
    token: YOUR_ARONA_BOT_TOKEN
```

#### LLM 設定例（`llm_config.yaml`）

```yaml
llm:
  model: "google/gemini-2.5-pro"
  # OpenRouter 利用例: model: "openrouter/free"
  max_images: 5
  max_images_per_request: 8
  # URL/添付画像の1枚あたり最大バイト数（超過時は DL 前またはストリーム中に拒否）
  max_image_bytes: 20971520  # 20 MiB
  providers:
    google:
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2  # レートリミット時に自動切替
    openrouter:
      base_url: https://openrouter.ai/api/v1
      api_key1: YOUR_OPENROUTER_KEY
      # 任意（ランキング用）
      http_referer: https://momoka-project.com
      x_title: Project MOMOKA
```

#### 画像生成（`images_config.yaml` / LLM 連携設定）

```yaml
# provider: "local" （内製）または "forge"
# モデル配置: models/image-models/<name>/<name>.safetensors
```

#### TTS（`tts_config.yaml`）

```yaml
tts:
  model_root: "models/tts-models"
  model_name: "your-model-name"
```

#### 音楽（`music_config.yaml`）

```yaml
music:
  default_volume: 20
  max_queue_size: 10000
  # メモリに保持するギルド再生状態の上限（上限到達時は非再生の最古状態を削除）
  max_guilds: 50
  auto_leave_timeout: 3
```

---

## コマンド一覧

### AI対話 (LLM)

| コマンド | 説明 |
|---------|------|
| `@PLANA` / `@ARONA` `<メッセージ>` | メンションで AI 対話 |
| （DM への平文） | メンション不要で AI 対話（履歴あり） |
| `/chat <メッセージ>` | メンションなしで対話（履歴なし。DM でも履歴ヒントは出さない） |
| `/clear_history` | 会話履歴リセット |
| `/switch-models` | チャンネル専用モデル切替 |

※ feedback は LLM ツールとして呼び出されます。コマンド実行は Agent の command モードが担当します。
※ `max_tool_iterations`（既定 5）はツール往復の上限です。超過時は手元の検索結果などからツールなしで最終回答を生成します。
※ LLM 応答（待機・本文・分割続き・討論投稿）は既定で `@silent`（通知抑制）送信。

### 音楽

| コマンド | 説明 |
|---------|------|
| `/play` `/pause` `/resume` `/stop` `/skip` | 再生制御 |
| `/seek` `/volume` `/queue` `/shuffle` `/clear` `/remove` `/nowplaying` `/loop` | キュー・音量など |
| `/reload_yt_cookies` | YouTube cookie 再読込（Bot 運用者のみ。`admin_user_ids` または `support.developer_user_id`） |

YouTube cookie はプロジェクト直下の `youtube_cookies.txt`（または `youtube_cookie.txt` / `music.youtube_cookie_file`）を原本とし、**起動時に非空なら自動ロード**して `data/youtube_cookies.runtime.txt` へコピーします（原本の書き戻し破壊を防ぐ）。起動後にファイルを置き直したとき用に `/reload_yt_cookies`（Bot 運用者のみ）もあります。

Now Playing パネル（Components V2）: 曲名（##）直下にチャンネル、Progress はインラインコード1行（`バー 時間 / 総時間`）。位置は実 PCM フレーム基準。再生開始前に実音フレームをプライムしてから Discord へ流す（起動無音で数秒ズレるのを防止）。Pause / Skip / Stop（Confirm/Cancel）/ Loop / QLoop / QShuffle（待ちキューは Queue 差し替えなしの in-place シャッフル）。次曲があるときだけ下部にキュー（最大5曲＋ページング）を表示。URL 指定の `/play` は停止パネルに履歴 URL を残す。
プレイリスト取得上限は `music.max_playlist_items`（既定 10000）。
保持するギルド再生状態の上限は `music.max_guilds`（既定 50）。上限到達時は非再生の最古状態を削除し、削除完了後に新規状態を受け付けます。
音楽メッセージは既定で `@silent`（通知抑制）送信。

### 画像生成（PLANA）

| コマンド | 説明 |
|---------|------|
| `@PLANA` で画像生成を依頼 | AI 経由で生成 |

### TTS（PLANA）

| コマンド | 説明 |
|---------|------|
| `/say <テキスト>` | 読み上げ |
| `/tts-help` | TTS ヘルプ |

### ゲーム統計（PLANA）

| コマンド | 説明 |
|---------|------|
| `/r6s` / `/valorant` | プレイヤー統計 |

### 通知（PLANA）

| コマンド | 説明 |
|---------|------|
| `/earthquake_*` | 地震速報設定・履歴・`/earthquake_settings` など |
| `/twitch_add` `/twitch_remove` `/twitch_list` | Twitch 通知 |

### Link Fix（PLANA）

| コマンド / 操作 | 説明 |
|----------------|------|
| SNS URL を投稿 | 元 embed を抑制し、Fix URL を silent 引用返信で置換 |
| `/linkfix` | 全体・サイト別・全サイト一括 on/off、Fix 元／Fix 先の設定（Components V2・Manage Server・デフォルト無効） |
| メッセージに `fxignore` | その投稿では Link Fix をスキップ |

### ユーティリティ

| コマンド | 説明 |
|---------|------|
| `/help` | ヘルプ（Components V2・app→guild→en 初期言語・🇯🇵/🇺🇸 切替・ページング） |
| `/invite` | PLANA / ARONA 招待（Components V2・app→guild→en） |
| `/updates` | GitHub コミット履歴（Components V2・全件取得・5件ページング・先頭/前/次/末尾） |
| `/download_video` `/download_audio` | メディアダウンロード（Components V2・Tunnel 一時共有・約 10 分で失効） |
| `/ping` `/serverinfo` `/userinfo` `/avatar` | 情報系（`/ping` は Gateway + Voice WebSocket） |
| `/latex <expression>` | LaTeX風数式をPNG化（matplotlib mathtext。フルLaTeXではない） |
| `/roll` `/diceroll` `/check` `/gacha` `/meow` `/support` `/feedback` | その他 |

---

## 機能詳細

### Agent ルーター

メンション・Bot への reply・/chat の前段で conversation / coding / command / unsupported に振り分けます。**NSFW / 成人向けリクエストは conversation に振り分け**（unsupported にはしない）。coding では冒頭の挨拶文のみ Discord 本文に出し、説明は `article.md`、各コードブロックは言語に応じた拡張子のファイルで添付します。ルーター応答に混入した `<thought>` / `<think>` は JSON 抽出前に除去し、thought 内の JSON も探索します。Google Gemma ルーター呼び出しでは `thinking_level=minimal` で思考出力を抑制します。失敗時は同一プロバイダーの API キーをすべて巡回し、その後に `fallback_models` へ進みます。ストリーム接続後に本文が空（Finish reason が `None` 等）の場合も、残りの `fallback_models` へ自動で切り替えます。


### APIキーローテーション

`llm_config.yaml` の各プロバイダに `api_key1`, `api_key2`, … を並べると、レートリミット／サーバーエラー／一時的な通信切断時に次のキーへ自動切替します（本チャットおよび Agent ルーター共通）。キーを使い切っても失敗する場合や、応答本文が空の場合は `fallback_models` の次候補へ進みます。待機 UI の予想応答時間は、記録キー（`provider/model`）と API 短名の表記ゆれを吸収して照合するため、フォールバック先モデルでも過去サンプルがあれば表示されます（サンプル不足時は「計測中...」）。

### 音楽

キュー最大 10,000 曲、ループ（OFF/ONE/ALL）、音量 0–200%、キュー終了時の VC 自動退出、VC 空室時の自動退出に対応。再生中は VC ステータスを `NowPlaying - 曲名` 形式で自動更新（ユーザーが手動編集した場合は以降 Bot は書き換えない。`Set Voice Channel Status` 権限が必要。権限不足時は INFO を1回だけ出し、以降は更新をサプレッション）。
**同一 VC 同居禁止:** PLANA と ARONA は同じボイスチャンネルに同時接続できません。既に同居している場合は ARONA のみ切断します。

**グレースフル再起動耐性:** `/shutdown`・GUI・Ctrl+C による終了時、接続中 VC の再生位置・キューを `data/momoka.db` の `vc_playback_sessions` に保存し、起動後に再 join して再生を再開します（**yt-dlp パイプの都合で途中シークはせず曲の先頭から**。再生開始成功で行削除）。ギルド／チャンネル未キャッシュや接続タイムアウトなどの一時失敗では行を残し、復元完了フラグを立てずに再試行します。強制終了／クラッシュは対象外。LLM 応答は自動再開せず、生成中メッセージを再起動案内に差し替えたうえで再メンションを待ちます。サポート誘導 URL は `utilities_config.yaml` の `support.discord_invite_url` / `developer_user_id` 等。

### 画像生成（PLANA）

`models/image-models/<name>/<name>.safetensors` を配置し、設定で `provider: "local"`（デフォルト）を使用。Forge 利用時は `--api` 付きで Forge を起動し `provider: "forge"` を設定。

### 地震速報（PLANA）

気象庁由来の情報を [P2P地震情報 JSON API v2 / WebSocket](https://www.p2pquake.net/develop/json_api_v2/#/) 経由で受信し、緊急地震速報（警報・code 556）・地震情報（code 551）・津波予報（code 552）を通知します。

- `/earthquake_settings` … 通知チャンネル（簡易で3種統一 / 詳細で個別）、震度レベル別フィルタ（震度不明含む）、津波通知 ON/OFF（Components V2・サーバー管理権限が必要）
- `/earthquake_channel` `/earthquake_remove` `/earthquake_test` … 通知先の設定・削除・テスト（サーバー管理権限が必要）
- 配信 embed フッターに `/earthquake_settings` への案内を表示
- EEW は予測地域・予測震度・発生時刻・主要動到達予測時刻を表示し、取消情報も通知します。API のテスト情報は本番チャンネルへ配信しません
- 発表検出のみの code 554 は通知しません。code 対応: 556=緊急地震速報（警報）、551=地震情報、552=津波予報
- P2P API 仕様上、EEW（556）の内容・配信品質は無保証であり、緊急地震速報（警報）としての公的利活用は非推奨です（[仕様](https://www.p2pquake.net/develop/json_api_v2/#/) 参照）
- 海外サーバーでも設定可能。取得・配信されるのは日本の地震・津波情報のみ
- 実装は `earthquake_constants.py`（共有定数）、`earthquake_map.py`（地図描画）、`earthquake_embeds.py`（通知表示）、`earthquake_protocol.py`（P2P API 接続）、`earthquake_commands.py`（コマンド）に責務を分離しています

### Link Fix（PLANA）

- 対象 URL を検知したら公式 embed を短時間待ち、**抑制（破壊）**してから Fix プロキシ URL を引用返信
- 返信は `@silent`。Fix 側に embed が付かない場合は返信削除＋元 embed 復元
- `/linkfix` でギルドごとに宗派（例: `fxtwitter.com` / `vxtwitter.com`）やマッチ元ドメインを変更可能。全サイト一括 on/off もあり（デフォルト無効・必要時に有効化）
- 元 embed 抑制には Manage Messages が必要

---

## トラブルシューティング

### ボットが起動しない

1. `configs/bots_config.yaml` の `bots.plana.token` / `bots.arona.token` を確認
2. Python 3.11.x か確認
3. 依存パッケージがインストールされているか確認
4. ルートに旧 `config.yaml` だけ置いても読み込まれません — `configs/` を使ってください

### AIが応答しない

1. `configs/llm_config.yaml` の API キーを確認
2. Developer Portal で **Message Content Intent** が両ボット有効か確認
3. 使用モデルが利用可能か確認


### 音楽が再生されない

1. ボイスチャンネル接続・FFmpeg・`youtube_cookies.txt` を確認
2. GUIの「TTS+Musicログ」で `BgUtils PO Token Provider v1.3.1 is ready` を確認
3. `http://127.0.0.1:4416/ping` がversion `1.3.1`を返すことを確認
4. Providerビルドが無い場合はNode.js 20+をPATHへ入れ、`startMOMOKA.bat`を再実行
5. `tv client ... DRM protected` が出る場合は、古い設定/プロセスではなく現行MOMOKAが起動したyt-dlpを使用しているか確認
6. `Voice WS timeout; reconnecting in …`（WARN）は discord.py の一時切断＋自動再接続。再接続後に再生が戻れば問題なし。ループする場合はネットワークを確認

### 画像生成ができない

1. PLANA 側で実行しているか（ARONA は誘導のみ）
2. モデル配置と `images` / LLM 画像設定を確認

### 地震速報が届かない

1. `/earthquake_status` で状態確認（PLANA）
2. `/earthquake_settings` で通知チャンネル・震度フィルタ・津波 ON/OFF を確認
3. WebSocket 接続を確認

---

## サポート

- Discord: [https://discord.com/invite/H79HKKqx3s](https://discord.com/invite/H79HKKqx3s)
- `/support` コマンド
- `/feedback` — 不具合・機能リクエストを Modal から開発者サーバーへ送信（LLM 会話からも可）

### フィードバック設定（セルフホスト）

`configs/utilities_config.yaml` の `feedback.channel_ids` に投稿先チャンネル ID を複数列挙します（Bot がその鯖にいる必要あり）。空のままでは `/feedback` と LLM `feedback` ツールは投稿できません。再起動通知などのサポート誘導 URL は同ファイルの `support.discord_invite_url` / `developer_user_id` / `developer_display_name` を編集します。

### ライセンス

- 本プロジェクト: **AGPL-3.0**
- Style-Bert-VITS2 統合部分: AGPL-3.0 / LGPL-3.0（`NOTICE` 参照）

---

**Made by [coffin299](https://discord.com/users/270446628622696449) & [zer0latency](https://discord.com/users/583206903442571264)**

&copy; 2026 MOMOKA
