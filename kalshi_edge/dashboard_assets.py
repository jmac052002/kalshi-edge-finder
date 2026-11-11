"""The stylesheet and script that ``kalshi_edge.dashboard`` inlines.

They live in Python string constants, not in ``.css`` and ``.js`` files, for
two reasons. The package has no package-data configuration, so a data file
next to the modules would be missing from an installed copy. And both strings
have to be byte-for-byte fixed: the page's Content-Security-Policy allows
exactly one style block and one script block, each by its SHA-256 hash, so
nothing built from report data may ever be formatted into them.

The script is progressive enhancement only. Every table and chart is already
complete HTML when the file is written, because some viewers (a chat app's
file preview, for one) do not run scripts at all. With scripts off the page
loses sorting, filtering, hover tooltips, and the "this file is old" banner,
and nothing else.

Colors: one blue for a positive edge and for plotted points, one red for a
negative edge, a gray midpoint, and four status colors that always appear
with an icon and a word. Text never takes a data color.
"""

from __future__ import annotations

CSS = """
:root {
  color-scheme: light;
  --page: #f4f3f0;
  --surface: #fcfcfb;
  --surface-2: #f0efec;
  --line: #dddcd6;
  --text: #0b0b0b;
  --text-2: #52514e;
  --text-3: #74736e;
  --pos: #2a78d6;
  --neg: #e34948;
  --good: #0ca30c;
  --warning: #fab219;
  --critical: #d03b3b;
  --focus: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #121211;
    --surface: #1a1a19;
    --surface-2: #262624;
    --line: #383835;
    --text: #f0efec;
    --text-2: #c3c2b7;
    --text-3: #9a998f;
    --pos: #3987e5;
    --neg: #e66767;
    --focus: #6da7ec;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1240px; margin: 0 auto; padding: 20px 16px 48px; }
h1 { font-size: 24px; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 0 0 4px; }
h3 { font-size: 15px; margin: 20px 0 8px; }
p { margin: 0 0 8px; }
.muted { color: var(--text-2); }
.small { font-size: 13px; }
.num { font-variant-numeric: tabular-nums; }
code, .tick { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12px; }
.tick { color: var(--text-3); overflow-wrap: anywhere; }
header.top { display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: flex-end; justify-content: space-between; }
nav.jump { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 14px 0 0; font-size: 14px; }
a { color: inherit; text-underline-offset: 3px; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, th:focus-visible,
[tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.banner {
  margin: 14px 0 0; padding: 10px 14px; border-radius: 8px;
  background: var(--surface); border: 1px solid var(--critical); border-left-width: 6px;
}
.banner[hidden] { display: none; }
.banner.warn { border-color: var(--warning); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 0; padding: 0; list-style: none; }
.chip {
  display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 999px; font-size: 13px;
}
.status { display: inline-flex; align-items: center; gap: 5px; font-weight: 600; white-space: nowrap; }
.status svg { width: 14px; height: 14px; flex: none; }
.st-good svg { fill: var(--good); }
.st-warning svg { fill: var(--warning); }
.st-critical svg { fill: var(--critical); }
.st-none svg { fill: var(--text-3); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 16px 0 0; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.tile .label { font-size: 13px; color: var(--text-2); }
.tile .value { font-size: 28px; font-weight: 650; line-height: 1.2; font-variant-numeric: tabular-nums; }
.tile .sub { font-size: 12px; color: var(--text-3); }
section.card {
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px 18px 20px; margin: 20px 0 0;
}
.facts { display: flex; flex-wrap: wrap; gap: 4px 22px; margin: 8px 0 0; padding: 0; list-style: none; font-size: 13px; }
.facts b { font-weight: 600; }
.controls { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; margin: 12px 0 8px; font-size: 14px; }
.controls label { display: inline-flex; align-items: center; gap: 6px; }
input[type="search"], select {
  font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 8px; min-width: 0;
}
input[type="search"] { width: 220px; max-width: 100%; }
.count { color: var(--text-2); font-size: 13px; margin-left: auto; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
.scroll.tall { max-height: 640px; overflow-y: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { padding: 7px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
tbody tr:last-child td { border-bottom: 0; }
thead th {
  position: sticky; top: 0; background: var(--surface-2); font-weight: 600; font-size: 13px;
  white-space: nowrap; z-index: 1;
}
th.r, td.r { text-align: right; }
td.r { font-variant-numeric: tabular-nums; white-space: nowrap; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable::after { content: " \\2195"; color: var(--text-3); font-weight: 400; }
th[aria-sort="ascending"]::after { content: " \\2191"; color: var(--text); }
th[aria-sort="descending"]::after { content: " \\2193"; color: var(--text); }
tr[hidden] { display: none; }
td .why { display: block; color: var(--text-3); font-size: 12px; max-width: 520px; }
td.title { min-width: 220px; }
.tag {
  display: inline-block; padding: 0 6px; border: 1px solid var(--line); border-radius: 4px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.03em; color: var(--text-2); background: var(--surface-2);
}
.tag + .tag { margin-left: 4px; }
.edge { display: inline-flex; align-items: center; gap: 8px; justify-content: flex-end; }
.edge span { min-width: 64px; }
svg.bar { width: 96px; height: 12px; flex: none; }
svg.bar .axis { stroke: var(--text-3); stroke-width: 1; }
svg.bar .pos, .key .pos { fill: var(--pos); }
svg.bar .neg, .key .neg { fill: var(--neg); }
.key { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--text-2); margin: 6px 0 0; }
.key span { display: inline-flex; align-items: center; gap: 5px; }
.key svg { width: 10px; height: 10px; }
.empty { padding: 14px; color: var(--text-2); background: var(--surface-2); border-radius: 8px; }
details { margin: 12px 0 0; }
summary { cursor: pointer; font-weight: 600; font-size: 14px; }
details ul { margin: 8px 0 0; padding-left: 20px; color: var(--text-2); font-size: 13px; }
.grid2 { display: grid; grid-template-columns: minmax(0, 380px) minmax(0, 1fr); gap: 20px; align-items: start; }
.grid2 table { font-size: 13px; }
.grid2 th, .grid2 td { padding: 6px 7px; }
.grid2 thead th { white-space: normal; }
.grid2 td:first-child { white-space: nowrap; }
.grid2 figure { max-width: 440px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: minmax(0, 1fr); } }
figure { margin: 0; }
figcaption { font-size: 12px; color: var(--text-3); margin-top: 4px; }
svg.chart { width: 100%; height: auto; display: block; }
svg.chart .grid { stroke: var(--line); stroke-width: 1; }
svg.chart .frame { fill: none; stroke: var(--line); stroke-width: 1; }
svg.chart .ideal { stroke: var(--text-3); stroke-width: 1; }
svg.chart .whisker { stroke: var(--pos); stroke-width: 2; stroke-linecap: round; }
svg.chart .dot { fill: var(--pos); stroke: var(--surface); stroke-width: 2; }
svg.chart .hit { fill: transparent; }
svg.chart .pt { outline: none; }
svg.chart .pt:hover .dot, svg.chart .pt:focus .dot { stroke: var(--text); }
svg.chart text { fill: var(--text-2); font-size: 11px; font-family: inherit; }
svg.chart text.axis-title { fill: var(--text); font-size: 12px; }
.events { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.event { border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.event h4 { margin: 0; font-size: 14px; }
.event table { margin-top: 8px; font-size: 13px; }
.event th, .event td { padding: 4px 6px; }
.event thead th { position: static; background: none; }
#tip {
  position: fixed; z-index: 10; pointer-events: none; max-width: 260px; padding: 6px 9px;
  background: var(--text); color: var(--surface); border-radius: 6px; font-size: 12px; line-height: 1.4;
  white-space: pre-line;
}
#tip[hidden] { display: none; }
footer { margin: 24px 0 0; color: var(--text-3); font-size: 12px; }
@media print {
  body { background: #fff; }
  .controls, nav.jump, #tip { display: none; }
  .scroll.tall { max-height: none; overflow: visible; }
  section.card { break-inside: avoid; }
}
"""

JS = r"""
(function () {
  "use strict";

  function cellValue(row, index) {
    var cell = row.cells[index];
    if (!cell) { return null; }
    var raw = cell.getAttribute("data-v");
    if (raw === null) { return cell.textContent.trim().toLowerCase(); }
    if (raw === "") { return null; }
    var number = Number(raw);
    return isNaN(number) ? raw.toLowerCase() : number;
  }

  // Empty cells sort last in both directions: "no number" is not "smallest".
  function compare(a, b, direction) {
    if (a === null && b === null) { return 0; }
    if (a === null) { return 1; }
    if (b === null) { return -1; }
    if (a < b) { return -direction; }
    if (a > b) { return direction; }
    return 0;
  }

  function sortBy(table, header) {
    var index = header.cellIndex;
    var descending = header.getAttribute("aria-sort") === "ascending" ||
      (header.getAttribute("aria-sort") === null && header.getAttribute("data-first") === "desc");
    var direction = descending ? -1 : 1;
    var body = table.tBodies[0];
    var rows = Array.prototype.slice.call(body.rows);
    var keyed = rows.map(function (row, position) {
      return { row: row, value: cellValue(row, index), position: position };
    });
    keyed.sort(function (a, b) {
      return compare(a.value, b.value, direction) || a.position - b.position;
    });
    keyed.forEach(function (item) { body.appendChild(item.row); });
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (cell) {
      cell.removeAttribute("aria-sort");
    });
    header.setAttribute("aria-sort", descending ? "descending" : "ascending");
  }

  Array.prototype.forEach.call(document.querySelectorAll("table.sort"), function (table) {
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (header) {
      if (!header.classList.contains("sortable")) { return; }
      header.tabIndex = 0;
      header.addEventListener("click", function () { sortBy(table, header); });
      header.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sortBy(table, header);
        }
      });
    });
  });

  // Filters. A control names its table with data-for and what it tests with
  // data-filter: "text" (row text contains), "flag" (row must have
  // data-<name>="1"), or "eq" (row's data-<name> equals the chosen value).
  function applyFilters(tableId) {
    var table = document.getElementById(tableId);
    if (!table) { return; }
    var controls = document.querySelectorAll('[data-for="' + tableId + '"]');
    var shown = 0;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var keep = true;
      Array.prototype.forEach.call(controls, function (control) {
        var kind = control.getAttribute("data-filter");
        var name = control.getAttribute("data-name");
        if (kind === "text") {
          var needle = control.value.trim().toLowerCase();
          if (needle && row.textContent.toLowerCase().indexOf(needle) === -1) { keep = false; }
        } else if (kind === "flag") {
          if (control.checked && row.getAttribute("data-" + name) !== "1") { keep = false; }
        } else if (kind === "eq") {
          if (control.value && row.getAttribute("data-" + name) !== control.value) { keep = false; }
        }
      });
      row.hidden = !keep;
      if (keep) { shown += 1; }
    });
    var counter = document.querySelector('[data-count="' + tableId + '"]');
    if (counter) {
      counter.textContent = shown + " of " + table.tBodies[0].rows.length + " rows";
    }
  }

  var filtered = {};
  Array.prototype.forEach.call(document.querySelectorAll("[data-for]"), function (control) {
    var tableId = control.getAttribute("data-for");
    filtered[tableId] = true;
    control.addEventListener("input", function () { applyFilters(tableId); });
    control.addEventListener("change", function () { applyFilters(tableId); });
  });
  Object.keys(filtered).forEach(applyFilters);

  // One tooltip element, filled as plain text and never parsed as markup.
  var tip = document.getElementById("tip");
  function showTip(target, x, y) {
    tip.textContent = target.getAttribute("data-tip");
    tip.hidden = false;
    var box = tip.getBoundingClientRect();
    var left = Math.min(Math.max(8, x + 12), window.innerWidth - box.width - 8);
    var top = y + 16;
    if (top + box.height > window.innerHeight - 8) { top = y - box.height - 12; }
    tip.style.left = left + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  if (tip) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-tip]"), function (target) {
      target.addEventListener("mousemove", function (event) { showTip(target, event.clientX, event.clientY); });
      target.addEventListener("mouseleave", function () { tip.hidden = true; });
      target.addEventListener("focus", function () {
        var box = target.getBoundingClientRect();
        showTip(target, box.left + box.width / 2, box.bottom);
      });
      target.addEventListener("blur", function () { tip.hidden = true; });
    });
  }

  // The file is a snapshot. Say how old it is now, and warn when it is old
  // enough that the prices in it should not be acted on.
  var built = document.querySelector("[data-built]");
  if (built) {
    var builtAt = Date.parse(built.getAttribute("data-built"));
    var limitHours = Number(built.getAttribute("data-stale-hours"));
    if (!isNaN(builtAt)) {
      var hours = (Date.now() - builtAt) / 3600000;
      var age = document.getElementById("built-age");
      if (age && hours >= 0) {
        var text;
        if (hours < 1) { text = Math.max(1, Math.round(hours * 60)) + " min ago"; }
        else if (hours < 48) { text = Math.round(hours) + " h ago"; }
        else { text = Math.round(hours / 24) + " days ago"; }
        age.textContent = " (" + text + ")";
      }
      var banner = document.getElementById("old-file");
      if (banner && limitHours > 0 && hours > limitHours) { banner.hidden = false; }
    }
  }
})();
"""
