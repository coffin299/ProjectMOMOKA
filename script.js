(function () {
    "use strict";

    var THEME_KEY = "momoka-theme";
    var LANG_KEY = "momoka-lang";

    function getPreferredTheme() {
        var stored = localStorage.getItem(THEME_KEY);
        if (stored === "light" || stored === "dark") {
            return stored;
        }
        return window.matchMedia("(prefers-color-scheme: dark)").matches
            ? "dark"
            : "light";
    }

    function getPreferredLang() {
        var stored = localStorage.getItem(LANG_KEY);
        if (stored === "ja" || stored === "en") {
            return stored;
        }
        var nav = (navigator.language || "ja").toLowerCase();
        return nav.startsWith("ja") ? "ja" : "en";
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        localStorage.setItem(THEME_KEY, theme);
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
        document.documentElement.setAttribute("data-lang", lang);
        document.documentElement.setAttribute("lang", lang);
        localStorage.setItem(LANG_KEY, lang);
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
        if (!menuBtn) {
            return;
        }

        function close() {
            document.body.classList.remove("docs-sidebar-open");
            menuBtn.setAttribute("aria-expanded", "false");
        }

        function toggle() {
            var open = document.body.classList.toggle("docs-sidebar-open");
            menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
        }

        menuBtn.addEventListener("click", toggle);
        if (overlay) {
            overlay.addEventListener("click", close);
        }

        document.querySelectorAll(".docs-nav a").forEach(function (link) {
            link.addEventListener("click", function () {
                if (window.matchMedia("(max-width: 800px)").matches) {
                    close();
                }
            });
        });

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
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
            document.querySelectorAll(".cmd-row").forEach(function (row) {
                var text = row.textContent.toLowerCase();
                row.style.display = !q || text.indexOf(q) !== -1 ? "" : "none";
            });

            document.querySelectorAll(".cmd-section").forEach(function (section) {
                var visible = Array.prototype.some.call(
                    section.querySelectorAll(".cmd-row"),
                    function (row) {
                        return row.style.display !== "none";
                    }
                );
                section.style.display = visible ? "" : "none";
            });
        });
    }
})();
