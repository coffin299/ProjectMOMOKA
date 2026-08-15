/**
 * Docs 向け Mermaid 描画 + 拡大縮小 / パン
 * 使い方: <script type="module" src="/mermaid-docs.js?v=..."></script>
 * 図は .docs-mermaid[data-mermaid-lang] 内の .mermaid に定義する
 */
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.6.0/dist/mermaid.esm.min.mjs";

var sources = new Map();
var zoomStates = new WeakMap();
var renderTimer = null;
var renderSeq = 0;
var MIN_SCALE = 0.75;
var MAX_SCALE = 3.5;
var STEP = 0.2;
var DEFAULT_SCALE = 1.35;

function cacheSources() {
  // 初回だけ定義テキストを保持する（描画後は SVG に置き換わるため）
  document.querySelectorAll(".docs-mermaid .mermaid").forEach(function (el) {
    if (!sources.has(el)) {
      sources.set(el, (el.textContent || "").trim());
    }
  });
}

function ensureChrome(fig) {
  // ツールバーとビューポートを一度だけ差し込む
  if (fig.querySelector(".docs-mermaid-viewport")) {
    return;
  }
  var mermaidEl = fig.querySelector(".mermaid");
  if (!mermaidEl) {
    return;
  }
  var caption = fig.querySelector("figcaption");

  var toolbar = document.createElement("div");
  toolbar.className = "docs-mermaid-toolbar";
  toolbar.innerHTML =
    '<button type="button" class="docs-mermaid-btn" data-zoom-out aria-label="Zoom out">−</button>' +
    '<span class="docs-mermaid-zoom-label" data-zoom-label>135%</span>' +
    '<button type="button" class="docs-mermaid-btn" data-zoom-in aria-label="Zoom in">+</button>' +
    '<button type="button" class="docs-mermaid-btn docs-mermaid-btn-reset" data-zoom-reset aria-label="Reset zoom">' +
    '<span class="lang-ja">リセット</span><span class="lang-en">Reset</span></button>' +
    '<span class="docs-mermaid-hint lang-ja">ホイールで拡大 · ドラッグで移動</span>' +
    '<span class="docs-mermaid-hint lang-en">Scroll to zoom · drag to pan</span>';

  var viewport = document.createElement("div");
  viewport.className = "docs-mermaid-viewport";
  var stage = document.createElement("div");
  stage.className = "docs-mermaid-stage";
  stage.appendChild(mermaidEl);
  viewport.appendChild(stage);

  fig.insertBefore(toolbar, caption || null);
  fig.insertBefore(viewport, caption || null);

  bindZoom(fig, viewport, stage, toolbar);
}

function getZoom(fig) {
  var state = zoomStates.get(fig);
  if (!state) {
    state = { scale: DEFAULT_SCALE, x: 0, y: 0 };
    zoomStates.set(fig, state);
  }
  return state;
}

function applyZoom(fig) {
  var state = getZoom(fig);
  var stage = fig.querySelector(".docs-mermaid-stage");
  var label = fig.querySelector("[data-zoom-label]");
  if (!stage) {
    return;
  }
  // 拡大縮小とパンを transform で適用
  stage.style.transform =
    "translate(" +
    state.x +
    "px, " +
    state.y +
    "px) scale(" +
    state.scale +
    ")";
  if (label) {
    label.textContent = Math.round(state.scale * 100) + "%";
  }
}

function clampScale(n) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, n));
}

function bindZoom(fig, viewport, stage, toolbar) {
  applyZoom(fig);

  toolbar.querySelector("[data-zoom-in]").addEventListener("click", function () {
    var state = getZoom(fig);
    state.scale = clampScale(state.scale + STEP);
    applyZoom(fig);
  });
  toolbar.querySelector("[data-zoom-out]").addEventListener("click", function () {
    var state = getZoom(fig);
    state.scale = clampScale(state.scale - STEP);
    applyZoom(fig);
  });
  toolbar.querySelector("[data-zoom-reset]").addEventListener("click", function () {
    var state = getZoom(fig);
    state.scale = DEFAULT_SCALE;
    state.x = 0;
    state.y = 0;
    applyZoom(fig);
  });

  // ホイールで拡大（ページスクロールを止める）
  viewport.addEventListener(
    "wheel",
    function (e) {
      e.preventDefault();
      var state = getZoom(fig);
      var delta = e.deltaY > 0 ? -STEP : STEP;
      state.scale = clampScale(state.scale + delta);
      applyZoom(fig);
    },
    { passive: false }
  );

  // ドラッグでパン
  var dragging = false;
  var lastX = 0;
  var lastY = 0;
  viewport.addEventListener("pointerdown", function (e) {
    if (e.button !== 0) {
      return;
    }
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
    viewport.classList.add("is-panning");
    viewport.setPointerCapture(e.pointerId);
  });
  viewport.addEventListener("pointermove", function (e) {
    if (!dragging) {
      return;
    }
    var state = getZoom(fig);
    state.x += e.clientX - lastX;
    state.y += e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;
    applyZoom(fig);
  });
  function endPan(e) {
    if (!dragging) {
      return;
    }
    dragging = false;
    viewport.classList.remove("is-panning");
    try {
      viewport.releasePointerCapture(e.pointerId);
    } catch (err) {}
  }
  viewport.addEventListener("pointerup", endPan);
  viewport.addEventListener("pointercancel", endPan);
}

function scheduleRender() {
  if (renderTimer) {
    clearTimeout(renderTimer);
  }
  renderSeq += 1;
  var seq = renderSeq;
  renderTimer = setTimeout(function () {
    renderMermaid(seq);
  }, 40);
}

async function renderMermaid(seq) {
  cacheSources();
  document.querySelectorAll(".docs-mermaid").forEach(ensureChrome);

  var theme =
    document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark"
      : "neutral";
  var lang =
    document.documentElement.getAttribute("data-lang") === "en" ? "en" : "ja";

  mermaid.initialize({
    startOnLoad: false,
    theme: theme,
    securityLevel: "strict",
    // 既定より大きめの文字で読みやすくする
    themeVariables: {
      fontSize: "18px",
      fontFamily:
        '"Noto Sans JP", "Inter", "Hiragino Sans", "Segoe UI", sans-serif',
    },
    flowchart: {
      htmlLabels: true,
      curve: "basis",
      nodeSpacing: 40,
      rankSpacing: 45,
      padding: 12,
    },
  });

  var figures = document.querySelectorAll(".docs-mermaid");
  var nodes = [];
  figures.forEach(function (fig) {
    var match = fig.getAttribute("data-mermaid-lang") === lang;
    fig.hidden = !match;
    var host = fig.querySelector(".mermaid");
    if (!host || !match) {
      return;
    }
    var src = sources.get(host);
    if (!src) {
      return;
    }
    var fresh = document.createElement("pre");
    fresh.className = "mermaid";
    fresh.textContent = src;
    host.replaceWith(fresh);
    sources.set(fresh, src);
    sources.delete(host);
    nodes.push(fresh);
  });

  if (!nodes.length || seq !== renderSeq) {
    return;
  }
  await mermaid.run({ nodes: nodes });

  // 再描画後もズーム状態を維持
  figures.forEach(function (fig) {
    if (!fig.hidden) {
      applyZoom(fig);
    }
  });
}

cacheSources();
scheduleRender();

new MutationObserver(scheduleRender).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ["data-theme", "data-lang"],
});
