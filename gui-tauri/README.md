# MOMOKA Host GUI (Tauri 2 + Svelte)

Bot 運用者向けホスト GUI。ギルド管理者ダッシュボードとは別系統です。

## 要件

- Node.js 20+
- Rust / cargo（[rustup](https://rustup.rs/)）
- Windows: WebView2 Runtime（通常は OS 同梱）

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

`startMOMOKA.bat` は release バイナリが無ければ上記相当を実行します。

## 構成

- `src/` … Svelte 5 UI
- `src-tauri/` … Rust（API 代理 / SSE / watchdog / Web タブ WebView / cookie 出力）
