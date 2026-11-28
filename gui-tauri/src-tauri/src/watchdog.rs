//! Host API / 親プロセス生存監視。Bot 停止後にウィンドウを残さない。

use crate::host_api::WATCH_TIMEOUT;
use crate::state::AppState;
use std::env;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

/// 起動直後は Bot / API 準備待ちで失敗しても落とさない
const GRACE_SECS: u64 = 15;
/// 一度も成功していないときの連続失敗上限
const FAIL_BEFORE_OK: u64 = 5;
/// 成功経験後の連続失敗上限
const FAIL_AFTER_OK: u64 = 3;

/// 1 秒間隔で /snapshot と親 PID を確認し、連続失敗で終了する。
pub fn start_api_watchdog(app: AppHandle, state: Arc<AppState>) {
    let parent_pid = env::var("MOMOKA_PARENT_PID")
        .ok()
        .and_then(|s| s.trim().parse::<u32>().ok())
        .filter(|p| *p > 0);

    tauri::async_runtime::spawn(async move {
        let started = Instant::now();
        loop {
            tokio::time::sleep(Duration::from_secs(1)).await;

            // 親 Python が死んでいれば即終了
            if let Some(pid) = parent_pid {
                if !parent_process_alive(pid) {
                    let _ = app.exit(0);
                    break;
                }
            }

            let client = state.client.clone();
            let url = format!("{}/snapshot", state.cfg.api_base());
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
            let in_grace = started.elapsed() < Duration::from_secs(GRACE_SECS);
            if in_grace {
                continue;
            }
            let seen_ok = state.api_seen_ok.load(Ordering::SeqCst);
            let limit = if seen_ok { FAIL_AFTER_OK } else { FAIL_BEFORE_OK };
            if streak >= limit {
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

/// 親プロセスが生存しているか。
fn parent_process_alive(pid: u32) -> bool {
    #[cfg(windows)]
    {
        windows_parent_alive(pid)
    }
    #[cfg(not(windows))]
    {
        // Linux は /proc、それ以外は親監視をスキップ（生存扱い）
        std::path::Path::new(&format!("/proc/{pid}")).exists()
            || !std::path::Path::new("/proc").exists()
    }
}

#[cfg(windows)]
fn windows_parent_alive(pid: u32) -> bool {
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    // GetExitCodeProcess の STILL_ACTIVE
    const STILL_ACTIVE: u32 = 259;

    unsafe {
        let handle = match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(h) => h,
            Err(_) => return false,
        };
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code).is_ok();
        let _ = CloseHandle(handle);
        ok && code == STILL_ACTIVE
    }
}
