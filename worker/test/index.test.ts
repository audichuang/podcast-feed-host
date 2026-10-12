// Offline check with a fake R2 bucket and a stubbed NAS fetch.
// Run: node --test worker/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.ts";

const TOK = "abcdefghijklmnopqrstuvwx"; // 24 chars of [a-z2-7]
const MP3 = `/feeds/${TOK}/EP01-0123abcd.mp3`;

const stream = (b: Uint8Array) => new Blob([b]).stream();

function fakeBucket(files: Record<string, Uint8Array>) {
  const meta = (k: string) => ({
    size: files[k].length,
    httpEtag: `"${k}"`,
    uploaded: new Date("2026-01-01T00:00:00Z"),
  });
  return {
    files,
    failNext: false,
    async head(k: string) {
      return files[k] ? meta(k) : null;
    },
    // Mirrors R2: onlyIf failure -> object without body; range start past EOF -> throws.
    async get(k: string, opts?: { range?: Headers; onlyIf?: Headers }) {
      if (this.failNext) {
        this.failNext = false;
        throw new Error("R2 internal error");
      }
      if (!files[k]) return null;
      const m = meta(k);
      const data = files[k];
      const inm = opts?.onlyIf?.get("If-None-Match");
      const im = opts?.onlyIf?.get("If-Match");
      if ((inm && inm === m.httpEtag) || (im && im !== m.httpEtag)) return m;
      const r = /^bytes=(\d*)-(\d*)$/.exec(opts?.range?.get("Range") ?? "");
      if (!r) return { ...m, body: stream(data) };
      if (r[1] === "") {
        const n = Number(r[2]);
        return { ...m, range: { suffix: n }, body: stream(data.slice(Math.max(0, data.length - n))) };
      }
      const start = Number(r[1]);
      if (start >= data.length) throw new Error("InvalidRange (10039)");
      const end = r[2] ? Math.min(Number(r[2]), data.length - 1) : data.length - 1;
      return {
        ...m,
        range: { offset: start, length: end - start + 1 },
        body: stream(data.slice(start, end + 1)),
      };
    },
    async list() {
      const objects = Object.keys(files).map((key) => ({
        key,
        size: files[key].length,
        uploaded: new Date("2026-01-01T00:00:00Z"),
      }));
      return { objects, truncated: false };
    },
    async delete(k: string) {
      delete files[k];
    },
    async put(k: string, body: ReadableStream) {
      files[k] = new Uint8Array(await new Response(body).arrayBuffer());
    },
  };
}

function env(files: Record<string, Uint8Array> = {}) {
  return { BUCKET: fakeBucket(files), UPLOAD_TOKEN: "s3cret", NAS_ORIGIN: "https://nas.test" };
}

let nasCalls: string[] = [];
let nasBodyBytes = 8;
let nasInits: RequestInit[] = [];
globalThis.fetch = (async (url: string, init: RequestInit) => {
  nasCalls.push(url);
  nasInits.push(init);
  return new Response("from-nas", {
    status: 200,
    headers: { "Content-Length": String(nasBodyBytes) },
  });
}) as typeof fetch;

let pending: Promise<unknown>[] = [];
const ctx = { waitUntil: (p: Promise<unknown>) => void pending.push(p) };
const call = (e: object, path: string, init: RequestInit = {}) =>
  worker.fetch(new Request(`https://podcast.test${path}`, init), e as never, ctx as never);

test("show.json and _trash are denied before any lookup or fallback", async () => {
  nasCalls = [];
  assert.equal((await call(env(), `/feeds/${TOK}/show.json`)).status, 403);
  assert.equal((await call(env(), `/_trash/x/show.json`)).status, 403);
  assert.equal((await call(env(), `/_trash/x/feed.xml`)).status, 404);
  assert.equal((await call(env(), `/_promote${MP3}`)).status, 404);
  assert.deepEqual(nasCalls, []);
});

test("R2 hit serves Range as 206 with path-derived headers", async () => {
  const e = env({ [MP3.slice(1)]: new Uint8Array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) });
  const r = await call(e, MP3, { headers: { Range: "bytes=2-5" } });
  assert.equal(r.status, 206);
  assert.equal(r.headers.get("Content-Range"), "bytes 2-5/10");
  assert.equal(r.headers.get("Content-Type"), "audio/mpeg");
  assert.match(r.headers.get("Cache-Control") ?? "", /immutable/);
  assert.deepEqual([...new Uint8Array(await r.arrayBuffer())], [2, 3, 4, 5]);
});

test("feed.xml gets RSS type and no-cache regardless of stored metadata", async () => {
  const key = `feeds/${TOK}/feed.xml`;
  const r = await call(env({ [key]: new TextEncoder().encode("<rss/>") }), `/${key}`);
  assert.equal(r.headers.get("Content-Type"), "application/rss+xml; charset=utf-8");
  assert.equal(r.headers.get("Cache-Control"), "no-cache");
});

test("R2 miss falls back to NAS; PUT never does", async () => {
  nasCalls = [];
  const r = await call(env(), MP3);
  assert.equal(await r.text(), "from-nas");
  assert.deepEqual(nasCalls, [`https://nas.test${MP3}`]);
  await call(env(), MP3, { method: "PUT", body: "x" });
  assert.equal(nasCalls.length, 1);
});

test("PUT mirrors the uploader: auth, allowlist, marker, 201", async () => {
  const e = env();
  const put = (path: string, auth: string) =>
    call(e, path, {
      method: "PUT",
      body: "abc",
      headers: { Authorization: auth, "Content-Length": "3" },
    });
  const bad = await put(MP3, "Bearer wrong");
  assert.equal(bad.status, 401);
  assert.equal(bad.headers.get("X-Podcast-Uploader"), "1");
  assert.equal((await put(`/feeds/${TOK}/evil.sh`, "Bearer s3cret")).status, 404);
  const ok = await put(MP3, "Bearer s3cret");
  assert.equal(ok.status, 201);
  assert.equal(new TextDecoder().decode(e.BUCKET.files[MP3.slice(1)]), "abc");
});

test("PUT over the size limit is 413", async () => {
  const r = await call(env(), MP3, {
    method: "PUT",
    headers: { Authorization: "Bearer s3cret", "Content-Length": String(101 * 1024 * 1024) },
  });
  assert.equal(r.status, 413);
});

test("healthz precheck: marker + 200 with right token, 401 with wrong", async () => {
  const ok = await call(env(), "/healthz", { headers: { Authorization: "Bearer s3cret" } });
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("X-Podcast-Uploader"), "1");
  const bad = await call(env(), "/healthz", { headers: { Authorization: "Bearer nope" } });
  assert.equal(bad.status, 401);
});

test("only a real listen (GET, >= 1 MB from NAS) leaves a promote marker", async () => {
  const e = env({ [`feeds/${TOK}/EP02-0123abcd.mp3`]: new Uint8Array([1]) });
  pending = [];
  nasBodyBytes = 2; // crawler probe: bytes=0-1
  await call(e, MP3, { headers: { Range: "bytes=0-1" } });
  nasBodyBytes = 5_000_000;
  await call(e, MP3, { method: "HEAD" }); // HEAD never counts
  await Promise.all(pending);
  assert.deepEqual(Object.keys(e.BUCKET.files).filter((k) => k.startsWith("_promote/")), []);
  await call(e, MP3); // real listen -> NAS
  await call(e, `/feeds/${TOK}/EP02-0123abcd.mp3`); // R2 hit
  await call(e, `/feeds/${TOK}/feed.xml`); // miss, but not evictable media
  await Promise.all(pending);
  const markers = Object.keys(e.BUCKET.files).filter((k) => k.startsWith("_promote/"));
  assert.deepEqual(markers, [`_promote${MP3}`]);
  nasBodyBytes = 8;
});

test("admin API: token required; list, read show.json, delete", async () => {
  const showKey = `feeds/${TOK}/show.json`;
  const e = env({ [showKey]: new TextEncoder().encode("{}"), [MP3.slice(1)]: new Uint8Array(3) });
  const auth = { Authorization: "Bearer s3cret" };
  assert.equal((await call(e, "/_admin/list")).status, 401);
  assert.equal((await call(e, `/_admin/object/${showKey}`)).status, 401);
  const list = await (await call(e, "/_admin/list", { headers: auth })).text();
  assert.equal(list, `2026-01-01T00:00:00.000Z|2|${showKey}\n2026-01-01T00:00:00.000Z|3${MP3.replace("/", "|")}\n`);
  assert.equal(await (await call(e, `/_admin/object/${showKey}`, { headers: auth })).text(), "{}");
  assert.equal((await call(e, `/feeds/${TOK}/show.json`)).status, 403); // public side still denied
  const del = await call(e, `/_admin/object${MP3}`, { method: "DELETE", headers: auth });
  assert.equal(del.status, 204);
  assert.deepEqual(Object.keys(e.BUCKET.files), [showKey]);
  assert.equal((await call(e, `/_admin/object${MP3}`, { method: "DELETE" })).status, 401);
});

test("publisher can PUT show.json even though reading it is 403", async () => {
  const e = env();
  const showPath = `/feeds/${TOK}/show.json`;
  const put = await call(e, showPath, {
    method: "PUT",
    body: "{}",
    headers: { Authorization: "Bearer s3cret", "Content-Length": "2" },
  });
  assert.equal(put.status, 201);
  assert.equal((await call(e, showPath)).status, 403);
  const anon = await call(e, showPath, { method: "PUT", body: "{}", headers: { "Content-Length": "2" } });
  assert.equal(anon.status, 401);
});

const TEN = () => env({ [MP3.slice(1)]: new Uint8Array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) });

test("unsatisfiable range is 416 with Content-Range */size; other R2 errors are 500", async () => {
  const r = await call(TEN(), MP3, { headers: { Range: "bytes=10-" } });
  assert.equal(r.status, 416);
  assert.equal(r.headers.get("Content-Range"), "bytes */10");
  const e = TEN();
  e.BUCKET.failNext = true;
  assert.equal((await call(e, MP3, { headers: { Range: "bytes=0-1" } })).status, 500);
});

test("multi-range or invalid Range gets the whole object as 200", async () => {
  const ranges = ["bytes=0-1,5-6", "abc", "bytes=5-2"];
  const responses = await Promise.all(ranges.map((Range) => call(TEN(), MP3, { headers: { Range } })));
  const sizes = await Promise.all(responses.map((r) => r.arrayBuffer().then((b) => b.byteLength)));
  for (const [i, r] of responses.entries()) {
    assert.equal(r.status, 200, ranges[i]);
    assert.equal(r.headers.get("Content-Range"), null);
    assert.equal(sizes[i], 10);
  }
});

test("suffix range and If-Range", async () => {
  const s = await call(TEN(), MP3, { headers: { Range: "bytes=-3" } });
  assert.equal(s.headers.get("Content-Range"), "bytes 7-9/10");
  const etag = `"${MP3.slice(1)}"`;
  const hit = await call(TEN(), MP3, { headers: { Range: "bytes=0-1", "If-Range": etag } });
  assert.equal(hit.status, 206);
  const miss = await call(TEN(), MP3, { headers: { Range: "bytes=0-1", "If-Range": '"stale"' } });
  assert.equal(miss.status, 200);
  assert.equal((await miss.arrayBuffer()).byteLength, 10);
});

test("conditionals: 304 for If-None-Match (GET and HEAD), 412 for failed If-Match, Last-Modified set", async () => {
  const etag = `"${MP3.slice(1)}"`;
  assert.equal((await call(TEN(), MP3, { headers: { "If-None-Match": etag } })).status, 304);
  assert.equal((await call(TEN(), MP3, { method: "HEAD", headers: { "If-None-Match": etag } })).status, 304);
  assert.equal((await call(TEN(), MP3, { headers: { "If-Match": '"other"' } })).status, 412);
  const r = await call(TEN(), MP3);
  assert.equal(r.headers.get("Last-Modified"), "Thu, 01 Jan 2026 00:00:00 GMT");
});

test("NAS fallback is not edge-cached, forwards If-Range, and passes redirects through", async () => {
  nasInits = [];
  await call(env(), MP3, { headers: { "If-Range": '"x"', Range: "bytes=0-1" } });
  const init = nasInits[0];
  assert.equal(init.cache, "no-store");
  assert.equal(init.redirect, "manual");
  assert.equal(new Headers(init.headers).get("If-Range"), '"x"');
});
