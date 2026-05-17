"use strict";
// Leaderboard: ~50k rows kept in memory, only a sliding window is in the DOM.
// Search is a case-insensitive substring filter over the in-memory array.

const STEP = 60; // rows added per window growth
const fmt = (n) => n.toLocaleString("en-US");
const esc = (s) =>
  s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const rowsEl = document.getElementById("rows");
const qEl = document.getElementById("q");
const countEl = document.getElementById("resultcount");

let all = []; // [author_id, screen_name, notes], pre-sorted by rank
let filtered = [];
let shown = 0;
let seeded = false; // load-in stagger only fires once

// rows: [author_id, screen_name, display_name, avatar_url, noted_tweets]
function rowHTML(r, rank, seed) {
  const cls = seed ? "row lgrid seed" : "row lgrid";
  const handle = r[1];
  const dname = (r[2] || "").trim();
  const avatar = r[3];
  const initial = esc((dname || handle || "?").charAt(0).toUpperCase() || "?");
  // The initial sits behind the <img>; if the image 404s (avatar changed /
  // account gone) the <img> removes itself and the monogram shows through.
  const av =
    `<span class="av" data-i="${initial}">` +
    (avatar
      ? `<img src="${esc(avatar)}" alt="" loading="lazy" ` +
        `referrerpolicy="no-referrer" onerror="this.remove()">`
      : "") +
    `</span>`;
  const nameLine =
    dname && dname.toLowerCase() !== handle.toLowerCase()
      ? `<span class="dname">${esc(dname)}</span>`
      : "";
  return (
    `<a class="${cls}" href="/a/${encodeURIComponent(r[0])}">` +
    `<span class="rank">${fmt(rank)}</span>` +
    av +
    `<span class="acct">${nameLine}<span class="handle">${esc(handle)}</span></span>` +
    `<span class="notes">${fmt(r[4])}</span></a>`
  );
}

function render(reset) {
  if (reset) {
    shown = Math.min(STEP, filtered.length);
    const seed = !seeded;
    let html = "";
    for (let i = 0; i < shown; i++) html += rowHTML(filtered[i], i + 1, seed && i < 24);
    rowsEl.innerHTML =
      html || `<div class="empty">No accounts match “${esc(qEl.value)}”.</div>`;
    if (seed && !seeded) {
      seeded = true;
      requestAnimationFrame(() => {
        rowsEl.querySelectorAll(".seed").forEach((el, i) => {
          el.style.transitionDelay = i * 22 + "ms";
          el.classList.add("in");
        });
      });
    }
  } else {
    // grow the window: append the next slice only
    const next = Math.min(shown + STEP, filtered.length);
    let html = "";
    for (let i = shown; i < next; i++) html += rowHTML(filtered[i], i + 1, false);
    rowsEl.insertAdjacentHTML("beforeend", html);
    shown = next;
  }
  countEl.textContent =
    filtered.length === all.length
      ? `${fmt(all.length)} accounts`
      : `${fmt(filtered.length)} of ${fmt(all.length)} accounts`;
}

function applyFilter() {
  const q = qEl.value.trim().toLowerCase();
  filtered = q
    ? all.filter(
        (r) =>
          r[1].toLowerCase().includes(q) ||
          (r[2] && r[2].toLowerCase().includes(q)),
      )
    : all;
  render(true);
}

let debounce;
qEl.addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(applyFilter, 90);
});

new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting && shown < filtered.length) render(false);
}).observe(document.getElementById("sentinel"));

const stamp = document.getElementById("stamp");
const toDisclaimer = () =>
  document.getElementById("disclaimer").scrollIntoView({ behavior: "smooth" });
stamp.addEventListener("click", toDisclaimer);
stamp.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    toDisclaimer();
  }
});

Promise.all([
  fetch("/meta.json").then((r) => r.json()),
  fetch("/leaderboard.json").then((r) => r.json()),
])
  .then(([meta, lb]) => {
    document.getElementById("stats").innerHTML = [
      [meta.tweets_tracked, "Noted posts tracked"],
      [meta.tweets_attributed, "Attributed to an account"],
      [meta.accounts_ranked, "Accounts ranked"],
    ]
      .map(([n, l]) => `<div class="stat"><div class="n">${fmt(n)}</div><div class="l">${l}</div></div>`)
      .join("");
    document.getElementById("disclaimer").textContent = meta.disclaimer;
    document.getElementById("dateline-date").textContent =
      "Updated " + new Date(meta.generated_at).toISOString().slice(0, 10);
    all = lb.rows;
    filtered = all;
    render(true);
  })
  .catch(() => {
    rowsEl.innerHTML = `<div class="empty">Could not load the ledger. Try again shortly.</div>`;
  });
