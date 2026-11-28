//! アプリ共有状態。

use crate::host_api::{build_client, HostConfig};
use parking_lot::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

/// YouTube / ニコニコ自動入力用クレデンシャル（フロントへ渡さない）
#[derive(Clone, Debug, Default)]
pub struct YtLogin {
    pub email: String,
    pub password: String,
    pub reload_minutes: u64,
    pub niconico_email: String,
    pub niconico_password: String,
}

/// SSE 購読の制御ハンドル
pub struct SseControl {
    /// 中断用
    pub stop_tx: Option<oneshot::Sender<()>>,
    /// バックグラウンドタスク
    pub join: Option<JoinHandle<()>>,
}

/// アプリ全体で共有する状態
pub struct AppState {
    pub cfg: HostConfig,
    pub client: reqwest::Client,
    pub yt_login: Mutex<YtLogin>,
    pub sse: Mutex<SseControl>,
    /// API watchdog が一度成功したか
    pub api_seen_ok: AtomicBool,
    /// 連続失敗回数
    pub api_fail_streak: AtomicU64,
    /// ブラウザ WebView が作成済みか
    pub browser_ready: AtomicBool,
    /// ブラウザを表示中か
    pub browser_visible: AtomicBool,
    /// cookie エクスポート用タイマー世代
    pub export_gen_youtube: AtomicU64,
    pub export_gen_niconico: AtomicU64,
}

impl AppState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            cfg: HostConfig::from_env(),
            client: build_client(),
            yt_login: Mutex::new(YtLogin {
                reload_minutes: 60,
                ..YtLogin::default()
            }),
            sse: Mutex::new(SseControl {
                stop_tx: None,
                join: None,
            }),
            api_seen_ok: AtomicBool::new(false),
            api_fail_streak: AtomicU64::new(0),
            browser_ready: AtomicBool::new(false),
            browser_visible: AtomicBool::new(false),
            export_gen_youtube: AtomicU64::new(0),
            export_gen_niconico: AtomicU64::new(0),
        })
    }

    pub fn bump_export_gen(&self, site: &str) -> u64 {
        match site {
            "niconico" => self.export_gen_niconico.fetch_add(1, Ordering::SeqCst) + 1,
            _ => self.export_gen_youtube.fetch_add(1, Ordering::SeqCst) + 1,
        }
    }

    pub fn current_export_gen(&self, site: &str) -> u64 {
        match site {
            "niconico" => self.export_gen_niconico.load(Ordering::SeqCst),
            _ => self.export_gen_youtube.load(Ordering::SeqCst),
        }
    }
}
