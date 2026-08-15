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
var MIN_SCALE = 0.5;
var MAX_SCALE = 4;
var STEP = 0.15;
var FIT_PAD = 16;
var MAX_VIEW_H = 520;

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
    state = { scale: 1, x: 0, y: 0, dirty: false, nw: 0, nh: 0 };
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
  // absolute + transform なのでレイアウトを膨らませない
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

function normalizeSvg(svg) {
  // Mermaid の SVG を viewBox 基準の実ピクセルに固定（100% 伸縮を防ぐ）
  var w = 0;
  var h = 0;
  var vb = svg.viewBox && svg.viewBox.baseVal;
  if (vb && vb.width > 0 && vb.height > 0) {
    w = vb.width;
    h = vb.height;
  } else {
    try {
      var box = svg.getBBox();
      w = box.width;
      h = box.height;
    } catch (err) {}
  }
  if (!(w > 0 && h > 0)) {
    w = svg.clientWidth || 320;
    h = svg.clientHeight || 180;
  }
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  svg.style.width = w + "px";
  svg.style.height = h + "px";
  svg.style.maxWidth = "none";
  svg.style.display = "block";
  return { w: w, h: h };
}

function fitCentered(fig) {
  // ビューポート内に全体が収まるよう拡大縮小し、中央へ。枠は視覚サイズに合わせる
  var viewport = fig.querySelector(".docs-mermaid-viewport");
  var stage = fig.querySelector(".docs-mermaid-stage");
  if (!viewport || !stage || fig.hidden) {
    return;
  }
  var svg = stage.querySelector("svg");
  if (!svg) {
    return;
  }

  stage.style.transform = "translate(0px, 0px) scale(1)";
  var natural = normalizeSvg(svg);
  var state = getZoom(fig);
  state.nw = natural.w;
  state.nh = natural.h;

  // 幅はコンテナ、高さ上限は画面の半分程度
  var availW = Math.max(viewport.clientWidth - FIT_PAD * 2, 80);
  var availH = Math.min(MAX_VIEW_H, Math.floor(window.innerHeight * 0.55)) - FIT_PAD * 2;
  if (availH < 120) {
    availH = 120;
  }

  // 全体が見える最大スケール（縮小も拡大も可）
  var scale = Math.min(availW / natural.w, availH / natural.h);
  scale = clampScale(scale);

  var visW = natural.w * scale;
  var visH = natural.h * scale;
  // 枠を図の見え方に合わせて小さくする（巨大な空白を作らない）
  viewport.style.height = Math.ceil(visH + FIT_PAD * 2) + "px";

  state.scale = scale;
  state.x = (viewport.clientWidth - visW) / 2;
  state.y = (viewport.clientHeight - visH) / 2;
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

  // 2 フレーム待ってから寸法確定 → フィット
  requestAnimationFrame(function () {
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
  });
}

cacheSources();
scheduleRender();

new MutationObserver(scheduleRender).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ["data-theme", "data-lang"],
});

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
