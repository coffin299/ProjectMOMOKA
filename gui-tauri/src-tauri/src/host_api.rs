//! Host GUI API への代理呼び出し（Bearer はここだけ保持）。

use serde::Serialize;
use serde_json::Value;
use std::env;
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

impl HostConfig {
    /// 環境変数から構築する。
    pub fn from_env() -> Self {
        // ホスト（既定 127.0.0.1）
        let host = env::var("MOMOKA_HOST_GUI_HOST").unwrap_or_else(|_| "127.0.0.1".into());
        // ポート（既定 18765）
        let port = env::var("MOMOKA_HOST_GUI_PORT").unwrap_or_else(|_| "18765".into());
        // トークン（無ければ空）
        let token = env::var("MOMOKA_HOST_GUI_TOKEN").unwrap_or_default();
        Self { host, port, token }
    }

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
pub struct ApiResponse {
    pub ok: bool,
    pub status: u16,
    pub json: Value,
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
        json,
        text,
    })
}
