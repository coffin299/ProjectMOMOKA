//! Host GUI API への代理呼び出し（Bearer はここだけ保持）。

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::Duration;

/// 代理 API の既定タイムアウト
const PROXY_TIMEOUT: Duration = Duration::from_secs(15);
/// 生存確認のタイムアウト
pub const WATCH_TIMEOUT: Duration = Duration::from_secs(3);

/// 起動時に環境変数から読む接続情報
#[derive(Clone, Debug)]
pub struct HostConfig {
    /// loopback ホスト
    pub host: String,
    /// Host GUI API ポート
    pub port: String,
    /// Bearer トークン（空なら未認証）
    pub token: String,
}

/// runner が書く接続情報ファイルの形
#[derive(Debug, Deserialize)]
struct ConnFile {
    host: Option<String>,
    port: Option<String>,
    token: Option<String>,
}

impl HostConfig {
    /// 環境変数・CLI・接続ファイルから構築する（CLI > env > ファイル）。
    pub fn from_env() -> Self {
        // 接続ファイル（DETACHED でも確実）
        let file_cfg = read_conn_file();
        // 環境変数
        let mut host = env::var("MOMOKA_HOST_GUI_HOST")
            .ok()
            .or_else(|| file_cfg.as_ref().and_then(|c| c.host.clone()))
            .unwrap_or_else(|| "127.0.0.1".into());
        let mut port = env::var("MOMOKA_HOST_GUI_PORT")
            .ok()
            .or_else(|| file_cfg.as_ref().and_then(|c| c.port.clone()))
            .unwrap_or_else(|| "18765".into());
        let mut token = env::var("MOMOKA_HOST_GUI_TOKEN")
            .ok()
            .or_else(|| file_cfg.as_ref().and_then(|c| c.token.clone()))
            .unwrap_or_default();
        // CLI で上書き（最優先）
        let args: Vec<String> = env::args().collect();
        let mut i = 1usize;
        while i < args.len() {
            let a = &args[i];
            if let Some(v) = a.strip_prefix("--momoka-host=") {
                host = v.to_string();
            } else if a == "--momoka-host" {
                if let Some(v) = args.get(i + 1) {
                    host = v.clone();
                    i += 1;
                }
            } else if let Some(v) = a.strip_prefix("--momoka-port=") {
                port = v.to_string();
            } else if a == "--momoka-port" {
                if let Some(v) = args.get(i + 1) {
                    port = v.clone();
                    i += 1;
                }
            } else if let Some(v) = a.strip_prefix("--momoka-token=") {
                token = v.to_string();
            } else if a == "--momoka-token" {
                if let Some(v) = args.get(i + 1) {
                    token = v.clone();
                    i += 1;
                }
            }
            i += 1;
        }
        Self { host, port, token }
    }
}

/// env 明示パス / exe 隣 / TEMP / data から接続ファイルを探す。
fn read_conn_file() -> Option<ConnFile> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    // runner が渡す絶対パスを最優先
    if let Ok(explicit) = env::var("MOMOKA_HOST_GUI_CONN_FILE") {
        if !explicit.trim().is_empty() {
            candidates.push(PathBuf::from(explicit));
        }
    }
    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("momoka-host-gui.conn.json"));
        }
    }
    if let Ok(tmp) = env::var("TEMP") {
        candidates.push(PathBuf::from(tmp).join("momoka-host-gui.conn.json"));
    }
    if let Ok(tmp) = env::var("TMP") {
        candidates.push(PathBuf::from(tmp).join("momoka-host-gui.conn.json"));
    }
    // cwd 相対（gui-tauri 起動時は効きにくいが保険）
    candidates.push(PathBuf::from("data").join("momoka-host-gui.conn.json"));
    for path in candidates {
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(cfg) = serde_json::from_str::<ConnFile>(&text) {
                return Some(cfg);
            }
        }
    }
    None
}

impl HostConfig {
    /// API ベース URL。
    pub fn api_base(&self) -> String {
        format!("http://{}:{}/host-gui/api", self.host, self.port)
    }

    /// Bearer が設定されているか。
    pub fn has_auth(&self) -> bool {
        !self.token.is_empty()
    }
}

/// IPC / 内部共用の API 応答
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiResponse {
    pub ok: bool,
    pub status: u16,
    /// レスポンス JSON（`json` は予約語衝突を避けるため data）
    pub data: Value,
    pub text: String,
}

/// shared reqwest クライアントを作る。
pub fn build_client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(PROXY_TIMEOUT)
        .no_proxy()
        .build()
        .expect("reqwest client")
}

/// Host API を呼ぶ。renderer 向け拒否リストを適用する。
pub async fn proxy_api(
    client: &reqwest::Client,
    cfg: &HostConfig,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    // パス検証
    if !path.starts_with('/') {
        return Err("invalid_path".into());
    }
    // パスワード経路はフロントの汎用 IPC から拒否
    if path == "/youtube-browser-login" || path.starts_with("/youtube-browser-login?") {
        return Err("forbidden".into());
    }
    request(client, cfg, method, path, body).await
}

/// 拒否リスト無しの内部呼び出し（cookie / ログイン設定用）。
pub async fn main_api(
    client: &reqwest::Client,
    cfg: &HostConfig,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    request(client, cfg, method, path, body).await
}

async fn request(
    client: &reqwest::Client,
    cfg: &HostConfig,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    let method = method.to_uppercase();
    let url = format!("{}{}", cfg.api_base(), path);
    let mut builder = match method.as_str() {
        "GET" => client.get(&url),
        "POST" => client.post(&url),
        "PUT" => client.put(&url),
        "DELETE" => client.delete(&url),
        other => return Err(format!("unsupported_method:{other}")),
    };
    if cfg.has_auth() {
        builder = builder.bearer_auth(&cfg.token);
    }
    if let Some(body) = body {
        if method != "GET" && method != "HEAD" {
            builder = builder.json(&body);
        }
    }
    let res = builder.send().await.map_err(|e| e.to_string())?;
    let status = res.status().as_u16();
    let ok = res.status().is_success();
    let text = res.text().await.map_err(|e| e.to_string())?;
    let json = if text.is_empty() {
        Value::Null
    } else {
        serde_json::from_str(&text).unwrap_or(Value::Null)
    };
    Ok(ApiResponse {
        ok,
        status,
        data: json,
        text,
    })
}
