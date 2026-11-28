# MOMOKA Host GUI (Tauri 2 + Svelte)

Bot 運用者向けホスト GUI。ギルド管理者ダッシュボードとは別系統です。

## 要件

- Node.js 20+（**初回ビルド時**）
- Rust / cargo（[rustup](https://rustup.rs/)、**初回ビルド時**）
- Visual Studio Build Tools（「C++ によるデスクトップ開発」、**初回ビルド時**）
- Windows: WebView2 Runtime（**実行時**。通常は OS 同梱）

`gui-tauri.exe` は `.gitignore` 対象です。別 PC では clone 後に必ず一度ビルドが必要です。

Host API 接続は `MOMOKA_HOST_GUI_*` 環境変数、`--momoka-port` / `--momoka-token` CLI、および `momoka-host-gui.conn.json`（exe 隣 / TEMP / data）の三重渡しです。メトリクスは `GET /host-gui/api/snapshot` を `momoka_snapshot` IPC で取得します。

ステータスバー:
- `link` … トークン有無（`ok :ポート` / `no-token` / `ipc-fail`）
- `api` … snapshot 成否（`ok` または短いエラー文）

寿命制御:
- Windows Job Object（親 Python 終了で GUI も終了）
- `MOMOKA_PARENT_PID` 監視
- Host API watchdog（起動 grace 後の連続失敗で終了）

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
