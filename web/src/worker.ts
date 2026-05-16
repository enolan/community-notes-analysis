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
  first_noted_at_ms: number | null;
  tweet_created_at: string | null;
  favorite_count: number | null;
}

const esc = (s: unknown): string =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!,
  );

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
      "SELECT screen_name FROM tweets WHERE author_id=?1 ORDER BY resolved_at DESC LIMIT 1",
    )
    .bind(authorId)
    .first<{ screen_name: string | null }>();

  if (!handleRow) {
    return notFound("No publicly-shown noted posts on file for this account.");
  }

  const { results } = await env.cn_leaderboard
    .prepare(
      "SELECT tweet_id, text, first_noted_at_ms, tweet_created_at, favorite_count " +
        "FROM tweets WHERE author_id=?1 ORDER BY first_noted_at_ms DESC",
    )
    .bind(authorId)
    .all<TweetRow>();

  const handle = handleRow.screen_name ?? "(unknown)";
  const records = results
    .map((t, i) => {
      const posted = fmtDate(
        t.tweet_created_at ? new Date(t.tweet_created_at) : null,
      );
      const noted = fmtDate(
        t.first_noted_at_ms ? new Date(t.first_noted_at_ms) : null,
      );
      const url = `https://x.com/i/web/status/${encodeURIComponent(t.tweet_id)}`;
      return (
        `<article class=record><div class=idx>${i + 1}</div><div>` +
        `<div class=body>${esc(t.text ?? "(text not captured)")}</div>` +
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
    `<p class=file-no>File № ${esc(authorId)}</p>` +
    `<h1 class=subject><span class=at>@</span>${esc(handle)}</h1>` +
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
