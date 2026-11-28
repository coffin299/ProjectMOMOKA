//! 子 WebView から cookie を読み取り Host API へ送る。

use crate::host_api::main_api;
use crate::state::AppState;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, Webview};

/// YouTube ログイン判定用 cookie 名
const YT_AUTH: &[&str] = &[
    "LOGIN_INFO",
    "SAPISID",
    "SID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
];
/// ニコニコログイン判定用
const NICO_AUTH: &[&str] = &["user_session"];

/// ドメイン許可（YouTube / Google 系）
pub fn yt_domain_ok(domain: &str) -> bool {
    host_in(
        domain,
        &[
            "youtube.com",
            "youtu.be",
            "google.com",
            "gstatic.com",
            "googleusercontent.com",
            "ggpht.com",
            "recaptcha.net",
        ],
    )
}

/// ドメイン許可（ニコニコ）
pub fn nico_domain_ok(domain: &str) -> bool {
    host_in(domain, &["nicovideo.jp", "nico.ms"])
}

fn host_in(domain: &str, suffixes: &[&str]) -> bool {
    let host = domain.trim_start_matches('.').to_ascii_lowercase();
    suffixes
        .iter()
        .any(|suffix| host == *suffix || host.ends_with(&format!(".{suffix}")))
}

/// debounce 付きでサイト cookie を送る。
pub fn schedule_site_export(app: AppHandle, state: Arc<AppState>, site: &str) {
    let site = site.to_string();
    let gen = state.bump_export_gen(&site);
    let app2 = app.clone();
    let state2 = state.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(2)).await;
        if state2.current_export_gen(&site) != gen {
            return;
        }
        if let Err(err) = export_site_cookies(&app2, &state2, &site, false).await {
            eprintln!("{site} cookie export failed: {err}");
            let msg = if site == "niconico" {
                "ニコニコの cookie 保存に失敗しました"
            } else {
                "YouTube の cookie 保存に失敗しました"
            };
            let _ = app2.emit(
                "momoka:youtube-cookie-status",
                json!({ "ok": false, "message": msg }),
            );
        }
    });
}

/// 指定サイトの cookie を API へ POST する。
pub async fn export_site_cookies(
    app: &AppHandle,
    state: &AppState,
    site: &str,
    quiet_if_logged_out: bool,
) -> Result<(), String> {
    let (domain_ok, auth_names, path, logged_out, saved, fresh, failed): (
        fn(&str) -> bool,
        &[&str],
        &str,
        &str,
        &str,
        &str,
        &str,
    ) = if site == "niconico" {
        (
            nico_domain_ok,
            NICO_AUTH,
            "/niconico-cookies",
            "ニコニコ動画にログインすると cookie を保存します",
            "ニコニコの cookie を保存しました",
            "ニコニコの cookie は最新です",
            "ニコニコの cookie 保存に失敗しました",
        )
    } else {
        (
            yt_domain_ok,
            YT_AUTH,
            "/youtube-cookies",
            "YouTube にログインすると cookie を保存します",
            "YouTube の cookie を保存しました",
            "YouTube の cookie は最新です",
            "YouTube の cookie 保存に失敗しました",
        )
    };

    let cookies = read_browser_cookies(app).await?;
    let auth: HashSet<&str> = auth_names.iter().copied().collect();
    let filtered: Vec<Value> = cookies
        .into_iter()
        .filter(|c| {
            let domain = c.get("domain").and_then(|v| v.as_str()).unwrap_or("");
            domain_ok(domain)
        })
        .collect();

    let logged_in = filtered.iter().any(|c| {
        c.get("name")
            .and_then(|v| v.as_str())
            .map(|n| auth.contains(n))
            .unwrap_or(false)
    });
    if !logged_in {
        if !quiet_if_logged_out {
            let _ = app.emit(
                "momoka:youtube-cookie-status",
                json!({ "ok": false, "message": logged_out }),
            );
        }
        return Ok(());
    }

    let res = main_api(
        &state.client,
        &state.cfg,
        "POST",
        path,
        Some(json!({ "cookies": filtered })),
    )
    .await?;
    if !res.ok {
        let _ = app.emit(
            "momoka:youtube-cookie-status",
            json!({ "ok": false, "message": failed }),
        );
        return Ok(());
    }
    if res.data.get("error").and_then(|v| v.as_str()) == Some("not_logged_in") {
        if !quiet_if_logged_out {
            let _ = app.emit(
                "momoka:youtube-cookie-status",
                json!({ "ok": false, "message": logged_out }),
            );
        }
        return Ok(());
    }
    let changed = res
        .data
        .get("changed")
        .and_then(|v| v.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
    if changed == 0 && res.data.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
        let _ = app.emit(
            "momoka:youtube-cookie-status",
            json!({ "ok": true, "message": fresh }),
        );
        return Ok(());
    }
    let ok = res.data.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let _ = app.emit(
        "momoka:youtube-cookie-status",
        json!({
            "ok": ok,
            "message": if ok { saved } else { failed }
        }),
    );
    Ok(())
}

/// ブラウザ WebView から cookie 一覧を取得する。
async fn read_browser_cookies(app: &AppHandle) -> Result<Vec<Value>, String> {
    let win = app
        .get_webview_window("momoka-browser")
        .ok_or_else(|| "browser_webview_missing".to_string())?;
    read_cookies_from_webview(win.as_ref()).await
}

async fn read_cookies_from_webview(webview: &Webview) -> Result<Vec<Value>, String> {
    #[cfg(windows)]
    {
        return read_cookies_windows(webview).await;
    }
    #[cfg(not(windows))]
    {
        let _ = webview;
        Ok(vec![])
    }
}

#[cfg(windows)]
async fn read_cookies_windows(webview: &Webview) -> Result<Vec<Value>, String> {
    use std::sync::mpsc;
    use webview2_com::{
        GetCookiesCompletedHandler, Microsoft::Web::WebView2::Win32::*,
    };
    use windows::core::Interface;

    let (tx, rx) = mpsc::channel::<Result<Vec<Value>, String>>();
    webview
        .with_webview(move |platform| {
            let send_err = |e: String| {
                let _ = tx.send(Err(e));
            };
            unsafe {
                let controller = platform.controller();
                let core = match controller.CoreWebView2() {
                    Ok(c) => c,
                    Err(e) => {
                        send_err(format!("CoreWebView2: {e}"));
                        return;
                    }
                };
                // CookieManager は ICoreWebView2_2 以降
                let core2: ICoreWebView2_2 = match core.cast() {
                    Ok(c) => c,
                    Err(e) => {
                        send_err(format!("cast ICoreWebView2_2: {e}"));
                        return;
                    }
                };
                let mgr = match core2.CookieManager() {
                    Ok(m) => m,
                    Err(e) => {
                        send_err(format!("CookieManager: {e}"));
                        return;
                    }
                };
                let tx2 = tx.clone();
                let handler =
                    GetCookiesCompletedHandler::create(Box::new(move |result, list| {
                        if let Err(e) = result {
                            let _ = tx2.send(Err(format!("GetCookies HRESULT {e:?}")));
                            return Ok(());
                        }
                        let Some(list) = list else {
                            let _ = tx2.send(Ok(vec![]));
                            return Ok(());
                        };
                        match collect_cookie_list(&list) {
                            Ok(v) => {
                                let _ = tx2.send(Ok(v));
                            }
                            Err(e) => {
                                let _ = tx2.send(Err(e));
                            }
                        }
                        Ok(())
                    }));
                if let Err(e) = mgr.GetCookies(windows_core::PCWSTR::null(), &handler) {
                    send_err(format!("GetCookies: {e}"));
                }
            }
        })
        .map_err(|e| e.to_string())?;

    for _ in 0..50 {
        if let Ok(v) = rx.try_recv() {
            return v;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    rx.recv_timeout(Duration::from_secs(2))
        .map_err(|_| "cookie_timeout".to_string())?
}

#[cfg(windows)]
unsafe fn collect_cookie_list(
    list: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2CookieList,
) -> Result<Vec<Value>, String> {
    use windows_core::{BOOL, PWSTR};

    let mut count = 0u32;
    list.Count(&mut count)
        .map_err(|e| format!("CookieList.Count: {e}"))?;
    let mut out = Vec::with_capacity(count as usize);
    for i in 0..count {
        let cookie = list
            .GetValueAtIndex(i)
            .map_err(|e| format!("GetValueAtIndex: {e}"))?;
        let mut name = PWSTR::null();
        let mut value = PWSTR::null();
        let mut domain = PWSTR::null();
        let mut path = PWSTR::null();
        cookie.Name(&mut name).map_err(|e| e.to_string())?;
        cookie.Value(&mut value).map_err(|e| e.to_string())?;
        cookie.Domain(&mut domain).map_err(|e| e.to_string())?;
        cookie.Path(&mut path).map_err(|e| e.to_string())?;
        let mut secure = BOOL(0);
        let mut is_session = BOOL(0);
        let mut expires = 0f64;
        let _ = cookie.IsSecure(&mut secure);
        let _ = cookie.IsSession(&mut is_session);
        let _ = cookie.Expires(&mut expires);
        out.push(json!({
            "name": pwstr_to_string(name),
            "value": pwstr_to_string(value),
            "domain": pwstr_to_string(domain),
            "path": pwstr_to_string(path),
            "secure": secure.as_bool(),
            "expires": if is_session.as_bool() { -1.0 } else { expires },
        }));
    }
    Ok(out)
}

#[cfg(windows)]
unsafe fn pwstr_to_string(p: windows_core::PWSTR) -> String {
    if p.is_null() {
        return String::new();
    }
    p.to_string().unwrap_or_default()
}
