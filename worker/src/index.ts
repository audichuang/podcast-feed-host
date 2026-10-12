// Podcast feed host on Cloudflare: R2 is the primary store, the NAS (Caddy via
// tunnel) is the fallback for anything R2 does not have.
//
// Read side mirrors Caddyfile; write side mirrors uploader/server.py so the
// publisher (notebooklm-mcp publish_series) only needs PODCAST_UPLOAD_URL changed.

export interface Env {
  BUCKET: R2Bucket;
  UPLOAD_TOKEN: string; // secret
  NAS_ORIGIN: string; // e.g. https://podcast-nas.example.com (separate tunnel hostname)
}

// Same allowlist as uploader/server.py — both segments are a trust boundary.
const TOKEN = "[a-z2-7]{24}";
const NAME =
  "(?:feed\\.xml|index\\.html|show\\.json|artwork(?:-[0-9a-f]{8})?\\.(?:png|jpg)|EP\\d{2}-[0-9a-f]{8}\\.(?:mp3|pdf|html)|EP\\d{2}-cover-[0-9a-f]{8}\\.(?:jpg|png))";
const PATH_RE = new RegExp(`^/feeds/(${TOKEN})/(${NAME})$`);
const SHOW_JSON_RE = /(^|\/)show\.json$/; // regex, not glob: must also match under _trash/
// Episode media is what the cache evicts; a NAS hit on one marks it for promotion
// back into R2 (the NAS daily job copies it up — see backup/r2-backup.sh).
const MEDIA_RE = new RegExp(`^/feeds/${TOKEN}/EP\\d{2}-[0-9a-f]{8}\\.(?:mp3|pdf|html)$`);

// ponytail: Worker request body limit is 100 MB on this zone plan. At 96k mono that is
// ~2.3 h of audio; a longer episode needs presigned R2 uploads instead.
const MAX_BYTES = 100 * 1024 * 1024;
// Generous: a busy NAS or a waking HDD measured up to ~19 s for a request (scan, 2026-10-12).
const NAS_HEADER_TIMEOUT_MS = 30_000;
// A NAS hit counts as "someone is listening" only when it actually ships audio. Podcast
// crawlers validate enclosures with HEAD or tiny ranges (bytes=0-1); counting those would
// mark every old episode daily and thrash the cache. 1 MB ≈ 1.5 min at 96k mono.
const PROMOTE_MIN_BYTES = 1_000_000;

const MARKER = { "X-Podcast-Uploader": "1" };

const TYPES: Record<string, string> = {
  mp3: "audio/mpeg",
  xml: "application/rss+xml; charset=utf-8",
  html: "text/html; charset=utf-8",
  jpg: "image/jpeg",
  png: "image/png",
  pdf: "application/pdf",
};

// Headers come from the path, never from stored metadata: objects migrated by
// rclone carry whatever type rclone guessed and no Cache-Control.
function contentHeaders(path: string): Headers {
  const h = new Headers();
  const ext = path.slice(path.lastIndexOf(".") + 1);
  if (TYPES[ext]) h.set("Content-Type", TYPES[ext]);
  if (ext === "mp3") h.set("Cache-Control", "public, max-age=31536000, immutable");
  if (path.endsWith("/feed.xml")) h.set("Cache-Control", "no-cache");
  h.set("Accept-Ranges", "bytes");
  return h;
}

async function tokenOk(got: string | null, secret: string): Promise<boolean> {
  // Hash both sides so lengths match, then compare without early exit.
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(got ?? "")),
    crypto.subtle.digest("SHA-256", enc.encode(`Bearer ${secret}`)),
  ]);
  const x = new Uint8Array(a);
  const y = new Uint8Array(b);
  let diff = got === null ? 1 : 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function reply(status: number, body: string | null = null): Response {
  return new Response(body, { status, headers: MARKER });
}

async function handlePut(req: Request, env: Env, path: string): Promise<Response> {
  if (!(await tokenOk(req.headers.get("Authorization"), env.UPLOAD_TOKEN))) return reply(401);
  if (!PATH_RE.test(path)) return reply(404);
  const len = Number(req.headers.get("Content-Length"));
  if (!req.headers.has("Content-Length") || !Number.isInteger(len)) return reply(411);
  if (len < 0 || len > MAX_BYTES) return reply(413);
  // R2 put is atomic: readers never see a half-written object (same guarantee as
  // the uploader's temp -> fsync -> rename).
  await env.BUCKET.put(path.slice(1), req.body, {
    httpMetadata: { contentType: contentHeaders(path).get("Content-Type") ?? undefined },
  });
  return reply(201);
}

async function fromR2(req: Request, env: Env, path: string): Promise<Response | null> {
  const key = path.slice(1);
  const headers = contentHeaders(path);
  if (req.method === "HEAD") {
    const obj = await env.BUCKET.head(key);
    if (!obj) return null;
    headers.set("ETag", obj.httpEtag);
    headers.set("Content-Length", String(obj.size));
    return new Response(null, { headers });
  }
  const obj = await env.BUCKET.get(key, { range: req.headers, onlyIf: req.headers });
  if (!obj) return null;
  headers.set("ETag", obj.httpEtag);
  if (!("body" in obj)) return new Response(null, { status: 304, headers });
  if (obj.range && req.headers.has("Range")) {
    const r = obj.range as { offset?: number; length?: number; suffix?: number };
    const start = r.suffix !== undefined ? obj.size - r.suffix : (r.offset ?? 0);
    const end = r.length !== undefined ? start + r.length - 1 : obj.size - 1;
    headers.set("Content-Range", `bytes ${start}-${end}/${obj.size}`);
    headers.set("Content-Length", String(end - start + 1));
    return new Response(obj.body, { status: 206, headers });
  }
  headers.set("Content-Length", String(obj.size));
  return new Response(obj.body, { headers });
}

async function fromNas(req: Request, env: Env, path: string): Promise<Response> {
  const fwd = new Headers();
  for (const h of ["Range", "If-None-Match", "If-Modified-Since"]) {
    const v = req.headers.get(h);
    if (v) fwd.set(h, v);
  }
  // Timeout only until headers arrive, so a dead NAS can't stall every miss while
  // a slow mp3 body is still allowed to stream.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), NAS_HEADER_TIMEOUT_MS);
  try {
    const res = await fetch(env.NAS_ORIGIN + path, {
      method: req.method,
      headers: fwd,
      signal: ctrl.signal,
    });
    return new Response(res.body, res);
  } catch {
    return new Response("origin unavailable\n", { status: 502 });
  } finally {
    clearTimeout(timer);
  }
}

// Admin API for the NAS backup job (backup/r2-backup.sh), same bearer token as uploads:
//   GET    /_admin/list          -> "uploaded|size|key" per line, oldest-first sortable
//   GET    /_admin/object/<key>  -> raw object (incl. show.json, which the public side 403s)
//   DELETE /_admin/object/<key>  -> 204
// The token can already overwrite every feed, so delete adds no new destructive power.
async function handleAdmin(req: Request, env: Env, path: string): Promise<Response> {
  if (!(await tokenOk(req.headers.get("Authorization"), env.UPLOAD_TOKEN))) return reply(401);
  if (path === "/_admin/list" && req.method === "GET") {
    const lines: string[] = [];
    let cursor: string | undefined;
    do {
      const page = await env.BUCKET.list({ cursor, limit: 1000 });
      for (const o of page.objects) lines.push(`${o.uploaded.toISOString()}|${o.size}|${o.key}`);
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor);
    return new Response(lines.join("\n") + (lines.length ? "\n" : ""), { headers: MARKER });
  }
  const key = path.startsWith("/_admin/object/") ? path.slice("/_admin/object/".length) : "";
  if (!key) return reply(404);
  if (req.method === "GET") {
    const obj = await env.BUCKET.get(key);
    if (!obj) return reply(404);
    return new Response(obj.body, { headers: { ...MARKER, "Content-Length": String(obj.size) } });
  }
  if (req.method === "DELETE") {
    await env.BUCKET.delete(key);
    return reply(204);
  }
  return reply(405);
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const path = new URL(req.url).pathname;

    // Deny rules run before any lookup, so the NAS fallback can't leak them.
    if (path === "/healthz") {
      if (req.method === "GET" && req.headers.has("Authorization")) {
        const ok = await tokenOk(req.headers.get("Authorization"), env.UPLOAD_TOKEN);
        return reply(ok ? 200 : 401);
      }
      return reply(200, "ok\n");
    }
    if (path.startsWith("/_admin/")) return handleAdmin(req, env, path);
    // Writes are token + allowlist gated on their own; the read deny rules below must not
    // apply to them (the publisher PUTs show.json on every publish).
    if (req.method === "PUT") return handlePut(req, env, path);
    if (req.method !== "GET" && req.method !== "HEAD") return new Response(null, { status: 405 });

    if (SHOW_JSON_RE.test(path)) return new Response(null, { status: 403 });
    if (path.startsWith("/_trash/") || path.startsWith("/_promote/"))
      return new Response(null, { status: 404 });

    // Fall back to the NAS only on "not in R2"; real R2 errors surface as 500.
    let r2: Response | null;
    try {
      r2 = await fromR2(req, env, path);
    } catch (e) {
      if (req.headers.has("Range")) return new Response(null, { status: 416 });
      console.error(JSON.stringify({ msg: "r2 error", path, err: String(e) }));
      return new Response(null, { status: 500 });
    }
    if (r2) return r2;
    const nas = await fromNas(req, env, path);
    const shipped = Number(nas.headers.get("Content-Length") ?? 0);
    if (req.method === "GET" && nas.ok && shipped >= PROMOTE_MIN_BYTES && MEDIA_RE.test(path)) {
      // Empty marker object; idempotent, so repeated plays cost one write each.
      ctx.waitUntil(env.BUCKET.put(`_promote${path}`, ""));
    }
    return nas;
  },
} satisfies ExportedHandler<Env>;
