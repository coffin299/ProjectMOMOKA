(function () {
    "use strict";

    var THEME_KEY = "momoka-theme";
    var LANG_KEY = "momoka-lang";

    function safeGet(key) {
        try {
            return localStorage.getItem(key);
        } catch (err) {
            return null;
        }
    }

    function safeSet(key, value) {
        try {
            localStorage.setItem(key, value);
            return true;
        } catch (err) {
            return false;
        }
    }

    function getPreferredTheme() {
        var stored = safeGet(THEME_KEY);
        if (stored === "light" || stored === "dark") {
            return stored;
        }
        try {
            return window.matchMedia("(prefers-color-scheme: dark)").matches
                ? "dark"
                : "light";
        } catch (err) {
            return "light";
        }
    }

    function getPreferredLang() {
        var stored = safeGet(LANG_KEY);
        if (stored === "ja" || stored === "en") {
            return stored;
        }
        var nav = (navigator.language || "ja").toLowerCase();
        return nav.startsWith("ja") ? "ja" : "en";
    }

    function applyTheme(theme) {
        if (theme !== "light" && theme !== "dark") {
            theme = "light";
        }
        document.documentElement.setAttribute("data-theme", theme);
        safeSet(THEME_KEY, theme);
        document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
            var isDark = theme === "dark";
            btn.setAttribute("aria-pressed", isDark ? "true" : "false");
            btn.textContent = isDark ? "Light" : "Dark";
            btn.setAttribute(
                "aria-label",
                isDark ? "Switch to light theme" : "Switch to dark theme"
            );
        });
    }

    function applyLang(lang) {
        if (lang !== "ja" && lang !== "en") {
            lang = "ja";
        }
        document.documentElement.setAttribute("data-lang", lang);
        document.documentElement.setAttribute("lang", lang);
        safeSet(LANG_KEY, lang);
        document.querySelectorAll("[data-lang-toggle]").forEach(function (btn) {
            btn.textContent = lang === "ja" ? "EN" : "JA";
            btn.setAttribute(
                "aria-label",
                lang === "ja" ? "Switch to English" : "Switch to Japanese"
            );
        });
    }

    // Apply early if not already set by inline head script
    if (!document.documentElement.getAttribute("data-theme")) {
        applyTheme(getPreferredTheme());
    } else {
        applyTheme(document.documentElement.getAttribute("data-theme"));
    }
    if (!document.documentElement.getAttribute("data-lang")) {
        applyLang(getPreferredLang());
    } else {
        applyLang(document.documentElement.getAttribute("data-lang"));
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var current =
                    document.documentElement.getAttribute("data-theme") ||
                    "light";
                applyTheme(current === "dark" ? "light" : "dark");
            });
        });

        document.querySelectorAll("[data-lang-toggle]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var current =
                    document.documentElement.getAttribute("data-lang") || "ja";
                applyLang(current === "ja" ? "en" : "ja");
            });
        });

        initDocsSidebar();
        initDocsToc();
        initCmdFilter();
    });

    function initDocsSidebar() {
        var menuBtn = document.querySelector("[data-docs-menu]");
        var overlay = document.querySelector(".docs-overlay");
        var sidebar = document.querySelector(".docs-sidebar");
        // メニューボタンが無いページ（LP等）では何もしない
        if (!menuBtn) {
            return;
        }

        // CSS のドロワー切替と同じブレークポイント
        var mq = window.matchMedia("(max-width: 800px)");

        // 旧バグ対策: inert は PC でもクリック不能になるため永久に外す
        if (sidebar) {
            sidebar.removeAttribute("inert");
        }

        // ドロワー閉じ。操作可否は CSS（pointer-events）に任せる
        function close() {
            document.body.classList.remove("docs-sidebar-open");
            menuBtn.setAttribute("aria-expanded", "false");
            if (sidebar) {
                // モバイル非表示時のみ a11y 上隠す。PC では常に公開
                sidebar.setAttribute("aria-hidden", mq.matches ? "true" : "false");
            }
            if (overlay) {
                overlay.setAttribute("aria-hidden", "true");
            }
        }

        // ドロワーを開く
        function openMenu() {
            document.body.classList.add("docs-sidebar-open");
            menuBtn.setAttribute("aria-expanded", "true");
            if (sidebar) {
                sidebar.removeAttribute("inert");
                sidebar.setAttribute("aria-hidden", "false");
            }
            if (overlay) {
                overlay.setAttribute("aria-hidden", "false");
            }
        }

        function toggle() {
            if (document.body.classList.contains("docs-sidebar-open")) {
                close();
            } else {
                openMenu();
            }
        }

        // 幅変更時に開閉状態と aria を同期
        function syncForViewport() {
            if (sidebar) {
                sidebar.removeAttribute("inert");
            }
            if (mq.matches) {
                close();
            } else {
                document.body.classList.remove("docs-sidebar-open");
                menuBtn.setAttribute("aria-expanded", "false");
                if (sidebar) {
                    sidebar.setAttribute("aria-hidden", "false");
                }
                if (overlay) {
                    overlay.setAttribute("aria-hidden", "true");
                }
            }
        }

        syncForViewport();
        if (typeof mq.addEventListener === "function") {
            mq.addEventListener("change", syncForViewport);
        } else if (typeof mq.addListener === "function") {
            mq.addListener(syncForViewport);
        }

        menuBtn.addEventListener("click", toggle);
        if (overlay) {
            overlay.addEventListener("click", close);
        }

        // モバイルでナビリンク押下後はドロワーを閉じる（遷移はブラウザ標準）
        document.querySelectorAll(".docs-nav a").forEach(function (link) {
            link.addEventListener("click", function () {
                if (mq.matches) {
                    close();
                }
            });
        });

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && mq.matches) {
                close();
            }
        });
    }

    function slugify(text) {
        return text
            .toLowerCase()
            .trim()
            .replace(/[^\w\u3040-\u30ff\u3400-\u9fff\s-]/g, "")
            .replace(/\s+/g, "-")
            .replace(/-+/g, "-")
            .slice(0, 80);
    }

    function initDocsToc() {
        var tocNav = document.querySelector("[data-docs-toc]");
        var article = document.querySelector(".docs-article");
        if (!tocNav || !article) {
            return;
        }

        var headings = article.querySelectorAll("h2, h3");
        if (!headings.length) {
            var tocAside = tocNav.closest(".docs-toc");
            if (tocAside) {
                tocAside.style.display = "none";
            }
            return;
        }

        function headingLabel(heading) {
            var lang =
                document.documentElement.getAttribute("data-lang") || "ja";
            var preferred = heading.querySelector(".lang-" + lang);
            if (preferred && preferred.textContent) {
                return preferred.textContent.trim();
            }
            return (heading.textContent || "").trim();
        }

        function rebuildTocLabels() {
            var links = tocNav.querySelectorAll("a");
            headings.forEach(function (heading, i) {
                if (links[i]) {
                    links[i].textContent = headingLabel(heading);
                }
            });
        }

        var frag = document.createDocumentFragment();
        headings.forEach(function (heading) {
            if (!heading.id) {
                heading.id = slugify(headingLabel(heading) || "section");
            }
            var a = document.createElement("a");
            a.href = "#" + heading.id;
            a.textContent = headingLabel(heading);
            a.className = heading.tagName === "H3" ? "toc-h3" : "toc-h2";
            frag.appendChild(a);
        });
        tocNav.appendChild(frag);

        document.querySelectorAll("[data-lang-toggle]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                setTimeout(rebuildTocLabels, 0);
            });
        });

        var links = tocNav.querySelectorAll("a");
        if (!("IntersectionObserver" in window)) {
            return;
        }

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (!entry.isIntersecting) {
                        return;
                    }
                    var id = entry.target.id;
                    links.forEach(function (link) {
                        link.classList.toggle(
                            "active",
                            link.getAttribute("href") === "#" + id
                        );
                    });
                });
            },
            {
                rootMargin: "-20% 0px -65% 0px",
                threshold: 0,
            }
        );

        headings.forEach(function (h) {
            observer.observe(h);
        });
    }

    function initCmdFilter() {
        var input = document.getElementById("cmd-filter");
        if (!input) {
            return;
        }

        input.addEventListener("input", function () {
            var q = (input.value || "").toLowerCase().trim();
            document.querySelectorAll(".cmd-section").forEach(function (section) {
                var heading = section.querySelector("h2, h3, .cmd-section-title, .cmd-cat");
                var sectionText = (section.textContent || "").toLowerCase();
                var headingMatch = !q || (heading && (heading.textContent || "").toLowerCase().indexOf(q) !== -1);
                var rows = section.querySelectorAll(".cmd-row");
                if (headingMatch && q) {
                    Array.prototype.forEach.call(rows, function (row) {
                        row.style.display = "";
                    });
                    section.style.display = "";
                    return;
                }
                var anyVisible = false;
                Array.prototype.forEach.call(rows, function (row) {
                    var text = row.textContent.toLowerCase();
                    var show = !q || text.indexOf(q) !== -1;
                    row.style.display = show ? "" : "none";
                    if (show) {
                        anyVisible = true;
                    }
                });
                section.style.display = !q || anyVisible || sectionText.indexOf(q) !== -1 ? "" : "none";
            });
        });
    }
})();
