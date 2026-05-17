/**
 * The Note Ledger — dynamic routes. Static assets (index.html, app.js,
 * styles.css, leaderboard.json, meta.json) are served by Workers Static
 * Assets; this Worker only runs for paths with no matching asset.
 *
 *   GET /a/:author_id   SSR account page from D1
 *   GET /@handle         302 -> /a/:author_id (latest handle wins)
 *   *                    404
 *
 * tweet `text` and `screen_name` are attacker-controlled — every
 * interpolation goes through esc().
 */

interface Env {
  cn_leaderboard: D1Database;
}

interface TweetRow {
  tweet_id: string;
  text: string | null;
  text_truncated: number | null;
  note_text: string | null;
  first_noted_at_ms: number | null;
  tweet_created_at: string | null;
  favorite_count: number | null;
}

// Notes longer than this get a no-JS "show more" clamp; shorter ones render
// whole. Roughly the 70th percentile of note length.
const NOTE_CLAMP = 360;

// Whether `text` is only the ~280-char head of a longer post. This can't be
// inferred from the stored text (the cut leaves no ellipsis/marker) — it's a
// resolve-time flag from the syndication payload. NULL means "short post,
// definitely complete".
const isTruncated = (t: TweetRow): boolean => t.text_truncated === 1;

function noteBlock(note: string | null, i: number): string {
  if (!note) return "";
  const head = "<div class=note-h>Community Note</div>";
  const body = `<div class=note-b>${esc(note)}</div>`;
  if (note.length <= NOTE_CLAMP) {
    return `<aside class=note>${head}${body}</aside>`;
  }
  // Checkbox-hack disclosure: works with zero JS, single text node.
  const id = `n${i}`;
  return (
    '<aside class="note note-clamp">' +
    `<input type=checkbox id=${id} class=note-tgl>` +
    head +
    body +
    `<label for=${id} class=note-more></label>` +
    "</aside>"
  );
}

const esc = (s: unknown): string =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!,
  );

// X profile images come back as the 48px `_normal` variant; bump to 400px
// for the page header. Leaves default-avatar / odd URLs untouched.
const bigAvatar = (u: string | null): string =>
  (u ?? "").replace(/_normal(\.\w+)(\?|$)/, "_400x400$1$2");

const MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");
function fmtDate(d: Date | null): string {
  if (!d || isNaN(d.getTime())) return "unknown";
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
const fmtNum = (n: number | null): string =>
  n == null ? "—" : n.toLocaleString("en-US");

const FONTS =
  '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' +
  '<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,560;1,6..72,400;1,6..72,560&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">';

function page(title: string, body: string, status = 200): Response {
  const html =
    "<!doctype html><html lang=en><head><meta charset=utf-8>" +
    '<meta name=viewport content="width=device-width,initial-scale=1">' +
    `<title>${esc(title)}</title>${FONTS}` +
    '<link rel=stylesheet href="/styles.css"></head><body><div class=wrap>' +
    body +
    "</div></body></html>";
  return new Response(html, {
    status,
    headers: {
      "content-type": "text/html;charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=600",
    },
  });
}

const FOOTER =
  "<footer>Counts are a lower bound. Deleted or suspended posts can't be " +
  "attributed to an account, and the most-noted accounts are the likeliest " +
  "to be gone — so this undercounts exactly the accounts most likely to " +
  "top the ledger.</footer>";

function mast(sub: string): string {
  return (
    '<header class=mast><div class=mast-rule></div>' +
    `<div class=dateline><span>The Note Ledger</span><span>${esc(sub)}</span></div>` +
    '<div class=mast-rule></div>'
  );
}

function notFound(msg: string): Response {
  return page(
    "Not on file — The Note Ledger",
    mast("Subject File") +
      "</header><main><article class=record><div class=idx>—</div><div>" +
      `<div class=body>${esc(msg)}</div></div></article></main>` +
      '<a class=back href="/">Back to the ledger</a>' +
      FOOTER,
    404,
  );
}

async function accountPage(env: Env, authorId: string): Promise<Response> {
  const handleRow = await env.cn_leaderboard
    .prepare(
      "SELECT screen_name, display_name, avatar_url FROM tweets " +
        "WHERE author_id=?1 ORDER BY resolved_at DESC LIMIT 1",
    )
    .bind(authorId)
    .first<{
      screen_name: string | null;
      display_name: string | null;
      avatar_url: string | null;
    }>();

  if (!handleRow) {
    return notFound("No publicly-shown noted posts on file for this account.");
  }

  const { results } = await env.cn_leaderboard
    .prepare(
      "SELECT tweet_id, text, text_truncated, note_text, first_noted_at_ms, " +
        "tweet_created_at, favorite_count " +
        "FROM tweets WHERE author_id=?1 ORDER BY first_noted_at_ms DESC",
    )
    .bind(authorId)
    .all<TweetRow>();

  const handle = handleRow.screen_name ?? "(unknown)";
  const dname = handleRow.display_name?.trim() || handle;
  const avatar = bigAvatar(handleRow.avatar_url);
  const avatarImg = avatar
    ? `<img class=avatar src="${esc(avatar)}" alt="" loading=lazy ` +
      'referrerpolicy=no-referrer onerror="this.remove()">'
    : "";
  const records = results
    .map((t, i) => {
      const posted = fmtDate(
        t.tweet_created_at ? new Date(t.tweet_created_at) : null,
      );
      const noted = fmtDate(
        t.first_noted_at_ms ? new Date(t.first_noted_at_ms) : null,
      );
      const url = `https://x.com/i/web/status/${encodeURIComponent(t.tweet_id)}`;
      const trunc = isTruncated(t)
        ? '<p class=trunc>Post truncated — X did not return the full text. ' +
          "Use “View original” for the rest.</p>"
        : "";
      return (
        `<article class=record><div class=idx>${i + 1}</div><div>` +
        `<div class=body>${esc(t.text ?? "(text not captured)")}</div>` +
        trunc +
        noteBlock(t.note_text, i) +
        '<div class=meta>' +
        `<span><span class=k>Posted</span> <time>${esc(posted)}</time></span>` +
        `<span><span class=k>First noted</span> <time>${esc(noted)}</time></span>` +
        `<span><span class=k>Likes</span> ${esc(fmtNum(t.favorite_count))}</span>` +
        `<a href="${esc(url)}" rel="nofollow noopener">View original →</a>` +
        '</div></div></article>'
      );
    })
    .join("");

  const body =
    mast("Subject File") +
    '<div class=ident>' +
    avatarImg +
    '<div class=ident-text>' +
    `<p class=file-no>File № ${esc(authorId)}</p>` +
    `<h1 class=subject>${esc(dname)}</h1>` +
    `<p class=at-line><span class=at>@</span>${esc(handle)}</p>` +
    '</div></div>' +
    `<p class=tally><b>${fmtNum(results.length)}</b> posts that earned a ` +
    "publicly-shown Community Note</p>" +
    '<a class=back href="/">Back to the ledger</a></header>' +
    `<main>${records}</main>` +
    FOOTER;

  return page(`@${handle} — The Note Ledger`, body);
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const path = decodeURIComponent(url.pathname);

    if (req.method !== "GET" && req.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // /a/:author_id  — ids are numeric
    const m = path.match(/^\/a\/(\d{1,25})$/);
    if (m) return accountPage(env, m[1]);

    // /@handle  -> redirect to the latest author_id for that handle
    const h = path.match(/^\/@([A-Za-z0-9_]{1,15})$/);
    if (h) {
      const row = await env.cn_leaderboard
        .prepare(
          "SELECT author_id FROM tweets WHERE screen_name=?1 COLLATE NOCASE " +
            "ORDER BY resolved_at DESC LIMIT 1",
        )
        .bind(h[1])
        .first<{ author_id: string }>();
      if (row) {
        return new Response(null, {
          status: 302,
          headers: { location: `/a/${encodeURIComponent(row.author_id)}` },
        });
      }
      return notFound(`No account on file for @${h[1]}.`);
    }

    return notFound("That page isn’t part of the ledger.");
  },
};
