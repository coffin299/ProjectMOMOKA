import { useEffect, useRef, useState } from "react";
import { getHostConfig } from "../api";

type WebViewEl = HTMLElement & {
  reload: () => void;
  goBack: () => void;
  goForward: () => void;
  loadURL: (url: string) => void;
  getURL: () => string;
};

const YOUTUBE = "https://www.youtube.com/";
const NICONICO = "https://www.nicovideo.jp/";

type Props = {
  visible: boolean;
};

export function WebPanel({ visible }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<WebViewEl | null>(null);
  const [url, setUrl] = useState(YOUTUBE);
  const [status, setStatus] = useState("YouTube かニコニコにログインすると cookie を保存します");

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const view = document.createElement("webview") as WebViewEl;
    view.setAttribute("src", YOUTUBE);
    view.setAttribute("partition", "persist:momoka-youtube");
    view.setAttribute("allowpopups", "true");
    view.className = "web-view";
    host.appendChild(view);
    viewRef.current = view;

    const onNav = () => {
      try {
        setUrl(view.getURL() || YOUTUBE);
      } catch {
        /* 破棄済み */
      }
    };
    view.addEventListener("did-navigate", onNav);
    view.addEventListener("did-navigate-in-page", onNav);

    const cfg = getHostConfig();
    const offStatus = cfg.onYoutubeCookieStatus?.((data) => {
      if (data?.message) setStatus(data.message);
    });
    const offReload = cfg.onYoutubeReload?.(() => {
      const current = viewRef.current;
      if (!current) return;
      let currentUrl = "";
      try {
        currentUrl = current.getURL() || "";
      } catch {
        return;
      }
      const onYoutube = currentUrl.includes("youtube.com");
      const onNiconico =
        currentUrl.includes("nicovideo.jp") && !currentUrl.includes("account.nicovideo.jp");
      if (onYoutube || onNiconico) current.reload();
    });

    return () => {
      offStatus?.();
      offReload?.();
      view.removeEventListener("did-navigate", onNav);
      view.removeEventListener("did-navigate-in-page", onNav);
      view.remove();
      viewRef.current = null;
    };
  }, []);

  return (
    <section className={`web-panel${visible ? "" : " hidden"}`} aria-hidden={!visible}>
      <div className="log-toolbar">
        <button type="button" className="tool-btn" onClick={() => viewRef.current?.goBack()}>
          戻る
        </button>
        <button type="button" className="tool-btn" onClick={() => viewRef.current?.goForward()}>
          進む
        </button>
        <button type="button" className="tool-btn" onClick={() => viewRef.current?.reload()}>
          再読込
        </button>
        <button
          type="button"
          className="tool-btn"
          onClick={() => viewRef.current?.loadURL(YOUTUBE)}
        >
          YouTube
        </button>
        <button
          type="button"
          className="tool-btn"
          onClick={() => viewRef.current?.loadURL(NICONICO)}
        >
          ニコニコ
        </button>
        <span className="muted web-url">{url}</span>
        <span className="muted web-status">{status}</span>
      </div>
      <div ref={hostRef} className="web-view-host" />
    </section>
  );
}
