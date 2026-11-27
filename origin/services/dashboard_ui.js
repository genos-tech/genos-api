/* Interactive parts of a Genos dashboard page (MIP-2452): collapse sections, show "+N more",
   sort and filter task lists, chart tooltips.

   This file is served AS IT IS, the same bytes for every page, in its own script element with the
   load's CSP nonce (services/html_note_pages.document), and only for a page the server drew.
   Nothing is ever built into it. What it needs, it reads from `data-gd-*` attributes the renderer
   wrote and escaped (services/dashboard_page lists them), and it only moves, hides and shows that
   markup: words go in with textContent, never as HTML. It makes no request, stores nothing, and
   does not touch `parent`, `top` or `location`. Without it the page is complete: it only adds the
   controls. A saved page may be older than this file, so a hook that is missing is not an error. */
(function () {
    "use strict";
    var own = document.currentScript;
    if (own && own.parentNode) own.parentNode.removeChild(own);

    var tip = null;
    var tipFor = null;
    var tipByKey = false;
    var pointer = "mouse"; /* a `click` does not say, in every browser, what made it */

    function all(root, selector) {
        return Array.prototype.slice.call(root.querySelectorAll(selector));
    }
    function attr(el, name) {
        return el.getAttribute(name) || "";
    }
    function pressed(el) {
        return attr(el, "aria-pressed") === "true";
    }
    function expanded(el) {
        return attr(el, "aria-expanded") === "true";
    }
    function closest(target, selector) {
        return target && target.closest ? target.closest(selector) : null;
    }

    /* The element's own words become a button inside it, so the element keeps its role. */
    function intoButton(el, css, act) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = css;
        button.setAttribute("data-gd-act", act);
        while (el.firstChild) button.appendChild(el.firstChild);
        el.appendChild(button);
        return button;
    }

    /* -- upgrade ---------------------------------------------------------------------------- */

    function foldable(heading) {
        var body = heading.nextElementSibling;
        if (!body || !body.id) return;
        var button = intoButton(heading, "gd-fold", "fold");
        button.setAttribute("aria-expanded", "true");
        button.setAttribute("aria-controls", body.id);
        var mark = document.createElement("span");
        mark.className = "gd-chev";
        mark.setAttribute("aria-hidden", "true");
        button.appendChild(mark);
    }

    function toolbar(template) {
        template.parentNode.insertBefore(document.importNode(template.content, true), template);
        template.parentNode.removeChild(template);
    }

    function more(holder) {
        var list = document.getElementById(attr(holder, "data-gd-for"));
        if (!list || !list.querySelector("template[data-gd-extra]")) return;
        var label = holder.textContent;
        var button = intoButton(holder, "gd-link", "more");
        button.setAttribute("aria-expanded", "false");
        button.setAttribute("aria-controls", list.id);
        button.setAttribute("data-gd-label", label);
        button.setAttribute("data-gd-less", attr(holder, "data-gd-less"));
    }

    function sortableColumn(cell) {
        intoButton(cell, "gd-th", "col");
        var table = closest(cell, "table");
        all(table, "tbody tr").forEach(function (row, index) {
            row.setAttribute("data-gd-i", String(index));
        });
    }

    /* The `title` is the no-script tooltip. Ours replaces it, so it is moved, not copied. */
    function tips(root) {
        all(root, "[data-gd-tip][title]").forEach(function (el) {
            el.setAttribute("data-gd-tip", attr(el, "title"));
            el.removeAttribute("title");
        });
    }

    function upgrade() {
        var steps = [
            ["[data-gd-fold]", foldable],
            ["template[data-gd-tools]", toolbar],
            ["[data-gd-more]", more],
            ["th[data-gd-col]", sortableColumn],
            [
                "[data-gd-tips]",
                function (el) {
                    el.setAttribute("tabindex", "0");
                },
            ],
        ];
        steps.forEach(function (step) {
            all(document, step[0]).forEach(function (el) {
                try {
                    step[1](el);
                } catch (error) {
                    /* one part that cannot be upgraded stays as the server wrote it */
                }
            });
        });
        tips(document);
        document.documentElement.classList.add("gd-js");
    }

    /* -- lists ------------------------------------------------------------------------------ */

    /* The rows the page holds back, put into their lists (once), still hidden. */
    function load(section) {
        all(section, "template[data-gd-extra]").forEach(function (template) {
            var list = template.parentNode;
            var found = document.importNode(template.content, true);
            all(found, "li").forEach(function (row) {
                row.setAttribute("data-gd-x", "");
                row.hidden = true;
            });
            var anchor = template;
            for (var child = list.firstElementChild; child; child = child.nextElementSibling) {
                if (child.hasAttribute("data-gd-more")) anchor = child;
            }
            list.insertBefore(found, anchor);
            list.removeChild(template);
            tips(list);
        });
    }

    function chosen(section, dimension) {
        return all(section, "button[data-gd-act=chip]")
            .filter(function (b) {
                return attr(b, "data-gd-dim") === dimension && pressed(b);
            })
            .map(function (b) {
                return attr(b, "data-gd-val");
            });
    }

    function sortKey(section) {
        var found = all(section, "button[data-gd-act=sort]").filter(pressed)[0];
        return found ? attr(found, "data-gd-key") : "due";
    }

    function number(text) {
        var n = parseInt(text, 10);
        return isNaN(n) ? 100000 : n;
    }

    function compare(a, b) {
        return a < b ? -1 : a > b ? 1 : 0;
    }

    function order(key) {
        return function (a, b) {
            var first = 0;
            if (key === "status")
                first = number(attr(a, "data-gd-rank")) - number(attr(b, "data-gd-rank"));
            if (key === "owner")
                first =
                    number(attr(a, "data-gd-who").slice(1)) -
                    number(attr(b, "data-gd-who").slice(1));
            return (
                first ||
                compare(attr(a, "data-gd-due") || "9999", attr(b, "data-gd-due") || "9999") ||
                number(attr(a, "data-gd-i")) - number(attr(b, "data-gd-i"))
            );
        };
    }

    function listsOf(section) {
        var lists = [];
        all(section, "li[data-gd-row]").forEach(function (row) {
            if (lists.indexOf(row.parentNode) < 0) lists.push(row.parentNode);
        });
        return lists;
    }

    function isOpen(section, list) {
        return all(section, "button[data-gd-act=more]").some(function (b) {
            return attr(b, "aria-controls") === list.id && expanded(b);
        });
    }

    /* Shows the section's rows as its buttons say. While a filter or another order is on, every
       row that matches shows, the held-back ones too. */
    function apply(section) {
        var statuses = chosen(section, "st");
        var people = chosen(section, "who");
        var key = sortKey(section);
        var filtering = statuses.length > 0 || people.length > 0;
        var active = filtering || key !== "due";
        var total = 0;
        listsOf(section).forEach(function (list) {
            var open = isOpen(section, list);
            all(list, "li.gd-grp").forEach(function (head) {
                list.removeChild(head);
            });
            var anchor = null;
            var rows = [];
            for (var child = list.firstElementChild; child; child = child.nextElementSibling) {
                if (child.hasAttribute("data-gd-row")) rows.push(child);
                else if (!anchor) anchor = child;
            }
            var shown = 0;
            var group = null;
            rows.sort(order(key)).forEach(function (row) {
                var match =
                    (!statuses.length || statuses.indexOf(attr(row, "data-gd-st")) >= 0) &&
                    (!people.length || people.indexOf(attr(row, "data-gd-who")) >= 0);
                row.hidden = !match || (row.hasAttribute("data-gd-x") && !active && !open);
                list.insertBefore(row, anchor);
                if (row.hidden) return;
                shown += 1;
                if (key !== "status" && key !== "owner") return;
                var name = attr(row, key === "owner" ? "data-gd-name" : "data-gd-stn");
                if (name === group) return;
                group = name;
                var head = document.createElement("li");
                head.className = "gd-grp";
                head.setAttribute("dir", "auto");
                head.textContent = name;
                list.insertBefore(head, row);
            });
            total += shown;
            all(list, "li[data-gd-rest]").forEach(function (line) {
                line.hidden = !active && !open;
            });
            all(section, "button[data-gd-act=more]").forEach(function (b) {
                if (attr(b, "aria-controls") === list.id)
                    (closest(b, "[data-gd-lead]") || b.parentNode).hidden = active;
            });
            var card = closest(list, ".card");
            if (card) card.hidden = filtering && !shown;
        });
        var none = section.querySelector("[data-gd-none]");
        if (none) none.textContent = filtering && !total ? attr(none, "data-gd-none") : "";
        var clear = section.querySelector("button[data-gd-act=clear]");
        if (clear) clear.hidden = !filtering;
    }

    function tableSort(button) {
        var cell = button.parentNode;
        var table = closest(cell, "table");
        var body = table.tBodies[0];
        var was = attr(cell, "aria-sort");
        var next = was === "ascending" ? "descending" : was === "descending" ? "" : "ascending";
        all(table, "th").forEach(function (other) {
            other.removeAttribute("aria-sort");
        });
        if (next) cell.setAttribute("aria-sort", next);
        var column = number(attr(cell, "data-gd-col"));
        var collator = new Intl.Collator(document.documentElement.lang || "en", { numeric: true });
        function words(row) {
            var td = row.cells[column];
            /* "$18,000" sorts as 18000 */
            return td ? td.textContent.trim().replace(/(\d),(?=\d{3})/g, "$1") : "";
        }
        Array.prototype.slice
            .call(body.rows)
            .sort(function (a, b) {
                var by = next ? collator.compare(words(a), words(b)) : 0;
                if (next === "descending") by = -by;
                return by || number(attr(a, "data-gd-i")) - number(attr(b, "data-gd-i"));
            })
            .forEach(function (row) {
                body.appendChild(row);
            });
    }

    function act(button) {
        var what = attr(button, "data-gd-act");
        var section = closest(button, "section");
        if (what === "fold") {
            var body = document.getElementById(attr(button, "aria-controls"));
            var was = expanded(button);
            button.setAttribute("aria-expanded", was ? "false" : "true");
            if (body) body.hidden = was;
        } else if (what === "col") {
            tableSort(button);
        } else if (section) {
            load(section);
            if (what === "more") {
                var open = !expanded(button);
                button.setAttribute("aria-expanded", open ? "true" : "false");
                button.textContent = attr(button, open ? "data-gd-less" : "data-gd-label");
            } else if (what === "sort") {
                all(section, "button[data-gd-act=sort]").forEach(function (b) {
                    b.setAttribute("aria-pressed", b === button ? "true" : "false");
                });
            } else if (what === "chip") {
                button.setAttribute("aria-pressed", pressed(button) ? "false" : "true");
            } else if (what === "clear") {
                var first = section.querySelector("button[data-gd-act=chip]");
                all(section, "button[data-gd-act=chip]").forEach(function (b) {
                    b.setAttribute("aria-pressed", "false");
                });
                if (first) first.focus();
            }
            apply(section);
        }
    }

    /* -- tooltips --------------------------------------------------------------------------- */

    function hideTip() {
        if (tipFor) tipFor.classList.remove("gd-on");
        if (tip) tip.hidden = true;
        tipFor = null;
        tipByKey = false;
    }

    function placeTip() {
        var mark = tipFor.querySelector("b") || tipFor.querySelector("i") || tipFor;
        var box = mark.getBoundingClientRect();
        var wide = document.documentElement.clientWidth;
        tip.style.left = "0px";
        tip.style.top = "0px";
        var size = tip.getBoundingClientRect();
        var left = box.left + box.width / 2 - size.width / 2;
        var top = box.top - size.height - 6;
        if (top < 4) top = box.bottom + 6;
        tip.style.left = Math.max(8, Math.min(left, wide - size.width - 8)) + "px";
        tip.style.top = top + "px";
    }

    function showTip(el, byKey) {
        var words = attr(el, "data-gd-tip");
        if (!words) return;
        hideTip();
        if (!tip) {
            tip = document.createElement("div");
            tip.className = "gd-tip";
            /* the chart's own label already says these to a screen reader */
            tip.setAttribute("aria-hidden", "true");
            tip.setAttribute("dir", "auto");
            document.body.appendChild(tip);
        }
        tip.textContent = words;
        tip.hidden = false;
        tipFor = el;
        tipByKey = !!byKey;
        el.classList.add("gd-on");
        placeTip();
    }

    /* A bar is 10px high: a tap just above or below it still finds it. */
    function nearTip(x, y) {
        var best = null;
        var distance = 13;
        all(document, "[data-gd-tip]").forEach(function (el) {
            var box = el.getBoundingClientRect();
            if (!box.width || x < box.left - 2 || x > box.right + 2) return;
            var away = y < box.top ? box.top - y : y > box.bottom ? y - box.bottom : 0;
            if (away < distance) {
                best = el;
                distance = away;
            }
        });
        return best;
    }

    function itemsOf(chart) {
        var found = all(chart, "[data-gd-tip]");
        if (chart.hasAttribute("data-gd-tip")) found.unshift(chart);
        return found.filter(function (el) {
            return el.getBoundingClientRect().width > 0;
        });
    }

    function steps(event) {
        var chart = event.target;
        if (!chart || !chart.hasAttribute || !chart.hasAttribute("data-gd-tips")) return;
        var items = itemsOf(chart);
        if (!items.length) return;
        var back = document.documentElement.dir === "rtl" ? "ArrowRight" : "ArrowLeft";
        var on = back === "ArrowRight" ? "ArrowLeft" : "ArrowRight";
        var list = chart.tagName === "UL";
        var at = items.indexOf(tipFor);
        if (event.key === on || (list && event.key === "ArrowDown")) at += 1;
        else if (event.key === back || (list && event.key === "ArrowUp")) at -= 1;
        else if (event.key === "Home") at = 0;
        else if (event.key === "End") at = items.length - 1;
        else return;
        event.preventDefault();
        var next = items[Math.max(0, Math.min(at, items.length - 1))];
        if (next.scrollIntoView) next.scrollIntoView({ block: "nearest" });
        showTip(next, true);
    }

    /* -- events ----------------------------------------------------------------------------- */

    document.addEventListener("click", function (event) {
        var button = closest(event.target, "button[data-gd-act]");
        if (button) {
            hideTip();
            act(button);
            return;
        }
        if (closest(event.target, "a,button")) {
            hideTip();
            return;
        }
        var found = closest(event.target, "[data-gd-tip]") || nearTip(event.clientX, event.clientY);
        if (!found || (found === tipFor && pointer !== "mouse")) hideTip();
        else showTip(found, false);
    });
    document.addEventListener("pointerdown", function (event) {
        pointer = event.pointerType || "mouse";
    });
    document.addEventListener("pointerover", function (event) {
        if (event.pointerType !== "mouse") return;
        pointer = "mouse";
        var found = closest(event.target, "[data-gd-tip]");
        if (found && found !== tipFor) showTip(found, false);
    });
    document.addEventListener("pointerout", function (event) {
        if (event.pointerType !== "mouse" || !tipFor) return;
        if (!tipFor.contains(event.relatedTarget)) hideTip();
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") hideTip();
        else steps(event);
    });
    document.addEventListener("focusin", function (event) {
        var chart = event.target;
        if (!chart || !chart.hasAttribute || !chart.hasAttribute("data-gd-tips")) return;
        try {
            if (!chart.matches(":focus-visible")) return;
        } catch (error) {
            return;
        }
        var items = itemsOf(chart);
        if (items.length) showTip(items[0], true);
    });
    document.addEventListener("focusout", function () {
        if (tipByKey) hideTip();
    });
    addEventListener(
        "scroll",
        function () {
            /* a tooltip asked for by key follows its mark; any other goes away */
            if (tipByKey && tipFor) placeTip();
            else hideTip();
        },
        true
    );
    addEventListener("resize", hideTip);
    document.addEventListener("DOMContentLoaded", upgrade);
})();
