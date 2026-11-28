# MOMOKA Host GUI (Tauri 2 + Svelte)

Bot 運用者向けホスト GUI。ギルド管理者ダッシュボードとは別系統です。

## 要件

- Node.js 20+（**初回ビルド時**）
- Rust / cargo（[rustup](https://rustup.rs/)、**初回ビルド時**）
- Visual Studio Build Tools（「C++ によるデスクトップ開発」、**初回ビルド時**）
- Windows: WebView2 Runtime（**実行時**。通常は OS 同梱）

`gui-tauri.exe` は `.gitignore` 対象です。別 PC では clone 後に必ず一度ビルドが必要です。

## アーキテクチャ（再構成）

Tauri は **シェル + Web タブ**専用。メトリクス / ログは Host API へ **HTTP 直結**します。

1. FastAPI が `gui-tauri/build` を `http://127.0.0.1:<port>/` で配信
2. Tauri 起動時にメイン WebView を `http://127.0.0.1:<port>/?t=<token>` へ navigate
3. フロントは `fetch(/host-gui/api/snapshot)` と `EventSource(/logs/stream?token=)` を使用
4. YouTube / ニコニコ WebView・cookie だけ Tauri IPC

接続情報は従来どおり env / CLI / conn JSON。`MOMOKA_PARENT_PID` + Job Object + API watchdog で孤児 GUI を防ぎます。

ステータスバー:
- `link` … セッション（`ok :ポート` / `no-token` 等）
- `api` … snapshot 成否（`ok` または短いエラー文）

`startMOMOKA.bat` は `gui-tauri/GUI_BUILD_ID` が変わると release を再ビルドします。

## 開発

```bash
cd gui-tauri
npm install
npm run tauri:dev
```

Host API が別途起動している必要があります（`MOMOKA_HOST_GUI_PORT` / `MOMOKA_HOST_GUI_TOKEN`）。

## 本番ビルド

```bash
cd gui-tauri
npm install
npm run tauri build -- --no-bundle
```

成果物: `src-tauri/target/release/gui-tauri.exe`

`startMOMOKA.bat` は初回起動時に release バイナリ（`gui-tauri.exe`）が無ければ上記相当を自動実行します。

## 構成

- `src/` … Svelte 5 UI
- `src-tauri/` … Rust（API 代理 / SSE / watchdog / Web タブ WebView / cookie 出力）
