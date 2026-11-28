//! Host API 生存監視。Bot 停止後にウィンドウを残さない。

use crate::host_api::WATCH_TIMEOUT;
use crate::state::AppState;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Manager};

/// 1 秒間隔で /status を確認し、連続失敗で終了する。
pub fn start_api_watchdog(app: AppHandle, state: Arc<AppState>) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(1)).await;
            let client = state.client.clone();
            let url = format!("{}/status", state.cfg.api_base());
            let mut req = client.get(&url).timeout(WATCH_TIMEOUT);
            if state.cfg.has_auth() {
                req = req.bearer_auth(&state.cfg.token);
            }
            let ok = match req.send().await {
                Ok(res) => res.status().is_success(),
                Err(_) => false,
            };
            if ok {
                state.api_seen_ok.store(true, Ordering::SeqCst);
                state.api_fail_streak.store(0, Ordering::SeqCst);
                continue;
            }
            let streak = state.api_fail_streak.fetch_add(1, Ordering::SeqCst) + 1;
            if state.api_seen_ok.load(Ordering::SeqCst) && streak >= 3 {
                // Bot 側停止とみなして終了
                let _ = app.exit(0);
                break;
            }
        }
    });
}

/// アプリ終了時のクリーンアップ入口。
pub async fn quit_host_gui(app: AppHandle, state: Arc<AppState>) {
    crate::sse::stop_log_sse(&state).await;
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.close();
    }
    app.exit(0);
}
