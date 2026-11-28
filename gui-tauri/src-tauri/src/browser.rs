//! YouTube / ニコニコ埋め込み WebView（メモリ抑制付き）。

use crate::cookies::{self, nico_domain_ok, yt_domain_ok};
use crate::host_api::main_api;
use crate::state::{AppState, YtLogin};
use serde::Deserialize;
use serde_json::json;
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;
use tauri::webview::PageLoadEvent;
use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder,
};
use url::Url;

const YOUTUBE: &str = "https://www.youtube.com/";
const NICONICO: &str = "https://www.nicovideo.jp/";
const BROWSER_LABEL: &str = "momoka-browser";

/// フロントから渡す表示領域
#[derive(Clone, Debug, Deserialize)]
pub struct BrowserBounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

/// Cloudflare challenge などログイン確認ホスト
fn login_challenge_ok(domain: &str) -> bool {
    let host = domain.trim_start_matches('.').to_ascii_lowercase();
    host == "challenges.cloudflare.com" || host.ends_with(".challenges.cloudflare.com")
}

/// Web タブで開いてよい URL か。
pub fn is_allowed_web_url(raw: &str) -> bool {
    let Ok(parsed) = Url::parse(raw) else {
        return false;
    };
    if parsed.scheme() != "https" {
        return false;
    }
    let Some(host) = parsed.host_str() else {
        return false;
    };
    yt_domain_ok(host) || nico_domain_ok(host) || login_challenge_ok(host)
}

/// 永続プロファイルディレクトリ。
fn browser_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?;
    let dir = base.join("momoka-browser-profile");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

/// 子 WebView を用意する（未作成なら作成、作成済みなら bounds 更新）。
pub fn ensure_browser(
    app: &AppHandle,
    state: &AppState,
    bounds: &BrowserBounds,
    visible: bool,
) -> Result<(), String> {
    if let Some(win) = app.get_webview_window(BROWSER_LABEL) {
        let _ = win.set_position(tauri::Position::Logical(LogicalPosition::new(
            bounds.x,
            bounds.y,
        )));
        let _ = win.set_size(tauri::Size::Logical(LogicalSize::new(
            bounds.width.max(1.0),
            bounds.height.max(1.0),
        )));
        if visible {
            let _ = win.show();
            state.browser_visible.store(true, Ordering::SeqCst);
        } else {
            let _ = win.hide();
            state.browser_visible.store(false, Ordering::SeqCst);
        }
        return Ok(());
    }

    let data_dir = browser_data_dir(app)?;
    let url = WebviewUrl::External(YOUTUBE.parse().map_err(|e: url::ParseError| e.to_string())?);
    let app_handle = app.clone();
    let state_arc = app.state::<Arc<AppState>>().inner().clone();

    let builder = WebviewWindowBuilder::new(app, BROWSER_LABEL, url)
        .title("MOMOKA Web")
        .parent(
            &app
                .get_webview_window("main")
                .ok_or_else(|| "main_window_missing".to_string())?,
        )
        .map_err(|e| e.to_string())?
        .position(bounds.x, bounds.y)
        .inner_size(bounds.width.max(1.0), bounds.height.max(1.0))
        .decorations(false)
        .resizable(false)
        .skip_taskbar(true)
        .visible(visible)
        .data_directory(data_dir)
        .on_navigation(|nav_url| is_allowed_web_url(nav_url.as_str()))
        .on_page_load(move |window, payload| {
            if payload.event() != PageLoadEvent::Finished {
                return;
            }
            let url = payload.url().to_string();
            let _ = app_handle.emit("momoka:browser-url", json!({ "url": url }));
            // 自動入力
            let login = state_arc.yt_login.lock().clone();
            let script = autofill_script(&login, payload.url().as_str());
            if let Some(script) = script {
                let _ = window.eval(&script);
            }
            // cookie エクスポートを予約
            cookies::schedule_site_export(app_handle.clone(), state_arc.clone(), "youtube");
            cookies::schedule_site_export(app_handle.clone(), state_arc.clone(), "niconico");
        });

    let win = builder.build().map_err(|e| e.to_string())?;
    if !visible {
        let _ = win.hide();
    }
    state.browser_ready.store(true, Ordering::SeqCst);
    state.browser_visible.store(visible, Ordering::SeqCst);
    Ok(())
}

/// 表示 / 非表示を切り替える（非表示時は hide でメモリ抑制）。
pub fn set_browser_visible(
    app: &AppHandle,
    state: &AppState,
    visible: bool,
    bounds: Option<BrowserBounds>,
) -> Result<(), String> {
    if visible {
        let bounds = bounds.ok_or_else(|| "bounds_required".to_string())?;
        ensure_browser(app, state, &bounds, true)?;
    } else if let Some(win) = app.get_webview_window(BROWSER_LABEL) {
        let _ = win.hide();
        state.browser_visible.store(false, Ordering::SeqCst);
        // 非表示中はキャッシュを捨ててメモリを抑える（cookie はプロファイル永続）
        let _ = win.eval("try { caches && caches.keys && caches.keys().then(ks => ks.forEach(k => caches.delete(k))); } catch (e) {}");
    } else if let Some(bounds) = bounds {
        ensure_browser(app, state, &bounds, false)?;
    }
    Ok(())
}

/// ブラウザ操作。
pub fn browser_navigate(app: &AppHandle, target: &str) -> Result<(), String> {
    let url = match target {
        "youtube" => YOUTUBE.to_string(),
        "niconico" => NICONICO.to_string(),
        other if is_allowed_web_url(other) => other.to_string(),
        _ => return Err("url_not_allowed".into()),
    };
    let win = app
        .get_webview_window(BROWSER_LABEL)
        .ok_or_else(|| "browser_webview_missing".to_string())?;
    let parsed: Url = url.parse().map_err(|e: url::ParseError| e.to_string())?;
    win.navigate(parsed).map_err(|e| e.to_string())
}

pub fn browser_reload(app: &AppHandle) -> Result<(), String> {
    let win = app
        .get_webview_window(BROWSER_LABEL)
        .ok_or_else(|| "browser_webview_missing".to_string())?;
    // 簡易 reload
    let url = win.url().map_err(|e| e.to_string())?;
    win.navigate(url).map_err(|e| e.to_string())
}

pub fn browser_eval_nav(app: &AppHandle, kind: &str) -> Result<(), String> {
    let win = app
        .get_webview_window(BROWSER_LABEL)
        .ok_or_else(|| "browser_webview_missing".to_string())?;
    let script = match kind {
        "back" => "history.back();",
        "forward" => "history.forward();",
        _ => return Err("unknown_nav".into()),
    };
    win.eval(script).map_err(|e| e.to_string())
}

pub fn browser_current_url(app: &AppHandle) -> Result<String, String> {
    let win = app
        .get_webview_window(BROWSER_LABEL)
        .ok_or_else(|| "browser_webview_missing".to_string())?;
    win.url()
        .map(|u| u.to_string())
        .map_err(|e| e.to_string())
}

/// /youtube-browser-login から設定を読む。
pub async fn load_yt_login(state: &AppState) {
    match main_api(
        &state.client,
        &state.cfg,
        "GET",
        "/youtube-browser-login",
        None,
    )
    .await
    {
        Ok(res) if res.ok => {
            let email = res
                .json
                .get("email")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let password = res
                .json
                .get("password")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let reload_minutes = res
                .json
                .get("reload_minutes")
                .and_then(|v| v.as_u64())
                .unwrap_or(60)
                .max(15);
            let niconico_email = res
                .json
                .get("niconico_email")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let niconico_password = res
                .json
                .get("niconico_password")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            *state.yt_login.lock() = YtLogin {
                email,
                password,
                reload_minutes,
                niconico_email,
                niconico_password,
            };
        }
        Ok(_) => eprintln!("YouTube login config unavailable: bad status"),
        Err(err) => eprintln!("YouTube login config unavailable: {err}"),
    }
}

/// 定期 reload + cookie 再送。
pub fn start_yt_reload_loop(app: AppHandle, state: Arc<AppState>) {
    tauri::async_runtime::spawn(async move {
        loop {
            let minutes = state.yt_login.lock().reload_minutes.max(15);
            tokio::time::sleep(Duration::from_secs(minutes * 60)).await;
            if let Some(win) = app.get_webview_window(BROWSER_LABEL) {
                // 表示中でなくても cookie 更新のため reload する
                if let Ok(url) = win.url() {
                    let s = url.as_str();
                    let on_youtube = s.contains("youtube.com");
                    let on_niconico =
                        s.contains("nicovideo.jp") && !s.contains("account.nicovideo.jp");
                    if on_youtube || on_niconico {
                        let _ = win.navigate(url);
                    }
                }
            }
            let _ = app.emit("momoka:youtube-reload", ());
            cookies::schedule_site_export(app.clone(), state.clone(), "youtube");
            cookies::schedule_site_export(app.clone(), state.clone(), "niconico");
        }
    });
}

/// 起動時キャッシュクリア（メインウィンドウ）。
pub fn clear_main_cache(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.eval(
            r#"try {
              if (window.caches) {
                caches.keys().then(keys => keys.forEach(k => caches.delete(k)));
              }
            } catch (e) {}"#,
        );
    }
}

fn autofill_script(login: &YtLogin, url: &str) -> Option<String> {
    if url.contains("account.nicovideo.jp") {
        if login.niconico_email.is_empty() || login.niconico_password.is_empty() {
            return None;
        }
        let email = serde_json::to_string(&login.niconico_email).ok()?;
        let password = serde_json::to_string(&login.niconico_password).ok()?;
        return Some(format!(
            r##"(() => {{
    if (window.__momokaNicoWatch) return "watching";
    window.__momokaNicoWatch = 1;
    const setValue = (el, value) => {{
      const proto = HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (el._valueTracker) el._valueTracker.setValue("");
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
      el.dispatchEvent(new Event("input", {{ bubbles: true }}));
      el.dispatchEvent(new Event("change", {{ bubbles: true }}));
    }};
    const tryFill = () => {{
      const mail = document.querySelector('input[name="mailOrTel"], input[autocomplete="username"]');
      const pass = document.querySelector('input[name="password"], input[autocomplete="current-password"]');
      if (!mail || !pass) return false;
      if (!pass.dataset.momokaFilled) {{
        pass.dataset.momokaFilled = "1";
        setValue(mail, {email});
        setValue(pass, {password});
      }}
      const form = pass.closest("form");
      const button = form && form.querySelector('button[type="submit"]');
      if (!button || button.disabled || button.dataset.momokaClicked) return false;
      button.dataset.momokaClicked = "1";
      button.click();
      return true;
    }};
    if (tryFill()) return "submit";
    const obs = new MutationObserver(() => {{
      if (tryFill()) obs.disconnect();
    }});
    obs.observe(document.documentElement, {{
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["disabled"],
    }});
    setTimeout(() => obs.disconnect(), 20000);
    return "wait";
  }})()"##
        ));
    }
    if !url.contains("accounts.google.com") {
        return None;
    }
    if login.email.is_empty() || login.password.is_empty() {
        return None;
    }
    let email = serde_json::to_string(&login.email).ok()?;
    let password = serde_json::to_string(&login.password).ok()?;
    Some(format!(
        r##"(() => {{
    const setValue = (el, value) => {{
      const proto = Object.getPrototypeOf(el);
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
      el.dispatchEvent(new Event("input", {{ bubbles: true }}));
      el.dispatchEvent(new Event("change", {{ bubbles: true }}));
    }};
    const emailBox = document.querySelector("#identifierId");
    if (emailBox && !emailBox.dataset.momokaFilled) {{
      emailBox.dataset.momokaFilled = "1";
      setValue(emailBox, {email});
      const next = document.querySelector("#identifierNext button, #identifierNext");
      if (next) next.click();
      return "email";
    }}
    const pass = document.querySelector('input[name="Passwd"]');
    if (pass && !pass.dataset.momokaFilled) {{
      pass.dataset.momokaFilled = "1";
      setValue(pass, {password});
      const next = document.querySelector("#passwordNext button, #passwordNext");
      if (next) next.click();
      return "password";
    }}
    return "none";
  }})()"##
    ))
}
