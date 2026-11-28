//! MOMOKA Host GUI（Tauri）。

mod browser;
mod cookies;
mod host_api;
mod sse;
mod state;
mod watchdog;

use browser::BrowserBounds;
use host_api::{fetch_snapshot, proxy_api, ApiResponse};
use serde_json::Value;
use state::AppState;
use std::sync::Arc;
use tauri::{AppHandle, Manager, State};

#[tauri::command]
async fn momoka_api(
    state: State<'_, Arc<AppState>>,
    method: Option<String>,
    path: String,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    let method = method.unwrap_or_else(|| "GET".into());
    proxy_api(&state.client, &state.cfg, &method, &path, body).await
}

#[tauri::command]
async fn momoka_snapshot(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    // Host API JSON をそのまま返す（ラッパ無し）
    fetch_snapshot(&state.client, &state.cfg).await
}

#[tauri::command]
fn momoka_has_auth(state: State<'_, Arc<AppState>>) -> bool {
    state.cfg.has_auth()
}

#[tauri::command]
fn momoka_conn_info(state: State<'_, Arc<AppState>>) -> Value {
    // トークン本体は返さない（接続診断用）
    serde_json::json!({
        "host": state.cfg.host,
        "port": state.cfg.port,
        "hasAuth": state.cfg.has_auth(),
        "apiBase": state.cfg.api_base(),
    })
}

#[tauri::command]
async fn momoka_sse_start(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    sse::start_log_sse(app, state.inner().clone()).await
}

#[tauri::command]
async fn momoka_sse_stop(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    sse::stop_log_sse(state.inner()).await;
    Ok(())
}

#[tauri::command]
async fn momoka_quit(app: AppHandle, state: State<'_, Arc<AppState>>) -> Result<(), String> {
    watchdog::quit_host_gui(app, state.inner().clone()).await;
    Ok(())
}

#[tauri::command]
fn browser_set_visible(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
    visible: bool,
    bounds: Option<BrowserBounds>,
) -> Result<(), String> {
    browser::set_browser_visible(&app, state.inner(), visible, bounds)
}

#[tauri::command]
fn browser_navigate(app: AppHandle, target: String) -> Result<(), String> {
    browser::browser_navigate(&app, &target)
}

#[tauri::command]
fn browser_reload(app: AppHandle) -> Result<(), String> {
    browser::browser_reload(&app)
}

#[tauri::command]
fn browser_go_back(app: AppHandle) -> Result<(), String> {
    browser::browser_eval_nav(&app, "back")
}

#[tauri::command]
fn browser_go_forward(app: AppHandle) -> Result<(), String> {
    browser::browser_eval_nav(&app, "forward")
}

#[tauri::command]
fn browser_get_url(app: AppHandle) -> Result<String, String> {
    browser::browser_current_url(&app)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = AppState::new();
    tauri::Builder::default()
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![
            momoka_api,
            momoka_snapshot,
            momoka_has_auth,
            momoka_conn_info,
            momoka_sse_start,
            momoka_sse_stop,
            momoka_quit,
            browser_set_visible,
            browser_navigate,
            browser_reload,
            browser_go_back,
            browser_go_forward,
            browser_get_url,
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            let state = app.state::<Arc<AppState>>().inner().clone();
            browser::clear_main_cache(&handle);
            watchdog::start_api_watchdog(handle.clone(), state.clone());
            tauri::async_runtime::spawn({
                let handle = handle.clone();
                let state = state.clone();
                async move {
                    browser::load_yt_login(&state).await;
                    browser::start_yt_reload_loop(handle.clone(), state.clone());
                    // 起動直後に既存ログイン cookie を送る
                    tokio::time::sleep(std::time::Duration::from_secs(8)).await;
                    let _ = cookies::export_site_cookies(&handle, &state, "youtube", true).await;
                    let _ = cookies::export_site_cookies(&handle, &state, "niconico", true).await;
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
