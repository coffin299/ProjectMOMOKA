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
var MIN_SCALE = 0.4;
var MAX_SCALE = 3.5;
var STEP = 0.15;
var DEFAULT_SCALE = 1;
var FIT_PAD = 20;

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
    '<span class="docs-mermaid-zoom-label" data-zoom-label>100%</span>' +
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
    state = { scale: DEFAULT_SCALE, x: 0, y: 0, dirty: false };
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
  // 拡大縮小とパンを transform で適用（原点は左上）
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

function measureStage(stage) {
  // scale(1) 換算の実寸を測る
  var svg = stage.querySelector("svg");
  var sw = stage.scrollWidth || stage.offsetWidth || 1;
  var sh = stage.scrollHeight || stage.offsetHeight || 1;
  if (svg) {
    try {
      var box = svg.getBBox();
      sw = Math.max(sw, box.width + box.x);
      sh = Math.max(sh, box.height + box.y);
    } catch (err) {}
    var aw = parseFloat(svg.getAttribute("width"));
    var ah = parseFloat(svg.getAttribute("height"));
    if (!isNaN(aw) && aw > 0) {
      sw = Math.max(sw, aw);
    }
    if (!isNaN(ah) && ah > 0) {
      sh = Math.max(sh, ah);
    }
  }
  return { sw: Math.max(sw, 1), sh: Math.max(sh, 1) };
}

function fitCentered(fig) {
  // 全体が収まるよう最大 100% までで縮小し、ビューポート中央へ置く
  var viewport = fig.querySelector(".docs-mermaid-viewport");
  var stage = fig.querySelector(".docs-mermaid-stage");
  if (!viewport || !stage || fig.hidden) {
    return;
  }
  stage.style.transform = "translate(0px, 0px) scale(1)";
  var size = measureStage(stage);
  var vw = viewport.clientWidth;
  var vh = viewport.clientHeight;
  if (vw < 8 || vh < 8) {
    return;
  }
  var scale = Math.min(
    DEFAULT_SCALE,
    (vw - FIT_PAD * 2) / size.sw,
    (vh - FIT_PAD * 2) / size.sh
  );
  scale = clampScale(scale);
  var state = getZoom(fig);
  state.scale = scale;
  state.x = (vw - size.sw * scale) / 2;
  state.y = (vh - size.sh * scale) / 2;
  state.dirty = false;
  applyZoom(fig);
}

function markDirty(fig) {
  getZoom(fig).dirty = true;
}

function bindZoom(fig, viewport, stage, toolbar) {
  toolbar.querySelector("[data-zoom-in]").addEventListener("click", function () {
    var state = getZoom(fig);
    state.scale = clampScale(state.scale + STEP);
    markDirty(fig);
    applyZoom(fig);
  });
  toolbar.querySelector("[data-zoom-out]").addEventListener("click", function () {
    var state = getZoom(fig);
    state.scale = clampScale(state.scale - STEP);
    markDirty(fig);
    applyZoom(fig);
  });
  toolbar.querySelector("[data-zoom-reset]").addEventListener("click", function () {
    fitCentered(fig);
  });

  // ホイールで拡大（ページスクロールを止める）
  viewport.addEventListener(
    "wheel",
    function (e) {
      e.preventDefault();
      var state = getZoom(fig);
      var delta = e.deltaY > 0 ? -STEP : STEP;
      state.scale = clampScale(state.scale + delta);
      markDirty(fig);
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
    markDirty(fig);
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
    themeVariables: {
      fontSize: "16px",
      fontFamily:
        '"Noto Sans JP", "Inter", "Hiragino Sans", "Segoe UI", sans-serif',
    },
    flowchart: {
      htmlLabels: true,
      curve: "basis",
      nodeSpacing: 36,
      rankSpacing: 40,
      padding: 10,
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

  // レイアウト確定後に fit（次フレームで寸法を取る）
  requestAnimationFrame(function () {
    if (seq !== renderSeq) {
      return;
    }
    figures.forEach(function (fig) {
      if (fig.hidden) {
        return;
      }
      var state = getZoom(fig);
      if (!state.dirty) {
        fitCentered(fig);
      } else {
        applyZoom(fig);
      }
    });
  });
}

cacheSources();
scheduleRender();

new MutationObserver(scheduleRender).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ["data-theme", "data-lang"],
});

// リサイズ時も未操作の図は再フィット
window.addEventListener("resize", function () {
  document.querySelectorAll(".docs-mermaid").forEach(function (fig) {
    if (fig.hidden) {
      return;
    }
    var state = getZoom(fig);
    if (!state.dirty) {
      fitCentered(fig);
    }
  });
});
