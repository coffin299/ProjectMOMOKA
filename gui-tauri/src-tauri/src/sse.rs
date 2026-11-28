//! Host GUI ログ SSE を Rust で購読し、フロントへ転送する。

use crate::state::AppState;
use futures_util::StreamExt;
use serde_json::Value;
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::oneshot;

/// SSE を開始する。既存購読があれば止める。
pub async fn start_log_sse(app: AppHandle, state: Arc<AppState>) -> Result<(), String> {
    stop_log_sse(&state).await;
    if !state.cfg.has_auth() {
        return Err("missing_token".into());
    }
    let (stop_tx, mut stop_rx) = oneshot::channel::<()>();
    let client = state.client.clone();
    let url = format!("{}/logs/stream", state.cfg.api_base());
    let token = state.cfg.token.clone();
    let app_clone = app.clone();

    let join = tokio::spawn(async move {
        let res = client
            .get(&url)
            .bearer_auth(&token)
            .header("Accept", "text/event-stream")
            .send()
            .await;
        let res = match res {
            Ok(r) if r.status().is_success() => r,
            Ok(r) => {
                let _ = app_clone.emit("momoka:sse-end", ());
                eprintln!("SSE HTTP {}", r.status());
                return;
            }
            Err(e) => {
                let _ = app_clone.emit("momoka:sse-end", ());
                eprintln!("SSE connect failed: {e}");
                return;
            }
        };
        let mut stream = res.bytes_stream();
        let mut buf = String::new();
        loop {
            tokio::select! {
                _ = &mut stop_rx => break,
                chunk = stream.next() => {
                    match chunk {
                        Some(Ok(bytes)) => {
                            buf.push_str(&String::from_utf8_lossy(&bytes));
                            while let Some(idx) = buf.find("\n\n") {
                                let frame = buf[..idx].to_string();
                                buf = buf[idx + 2..].to_string();
                                let data_lines: Vec<&str> = frame
                                    .lines()
                                    .filter(|ln| ln.starts_with("data:"))
                                    .map(|ln| ln[5..].trim_start())
                                    .collect();
                                if data_lines.is_empty() {
                                    continue;
                                }
                                let payload = data_lines.join("\n");
                                if let Ok(value) = serde_json::from_str::<Value>(&payload) {
                                    let _ = app_clone.emit("momoka:sse-log", value);
                                }
                            }
                        }
                        Some(Err(_)) | None => break,
                    }
                }
            }
        }
        let _ = app_clone.emit("momoka:sse-end", ());
    });

    let mut guard = state.sse.lock();
    guard.stop_tx = Some(stop_tx);
    guard.join = Some(join);
    // 未使用警告回避（Manager は Emitter 利用のため）
    let _ = app.try_state::<Arc<AppState>>();
    Ok(())
}

/// SSE を停止する。
pub async fn stop_log_sse(state: &AppState) {
    let (stop_tx, join) = {
        let mut guard = state.sse.lock();
        (guard.stop_tx.take(), guard.join.take())
    };
    if let Some(tx) = stop_tx {
        let _ = tx.send(());
    }
    if let Some(join) = join {
        let _ = join.await;
    }
}
