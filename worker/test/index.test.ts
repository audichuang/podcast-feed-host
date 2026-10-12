// Offline check with a fake R2 bucket and a stubbed NAS fetch.
// Run: node --test worker/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.ts";

const TOK = "abcdefghijklmnopqrstuvwx"; // 24 chars of [a-z2-7]
const MP3 = `/feeds/${TOK}/EP01-0123abcd.mp3`;

function fakeBucket(files: Record<string, Uint8Array>) {
  const meta = (k: string) => ({ size: files[k].length, httpEtag: `"${k}"` });
  return {
    files,
    async head(k: string) {
      return files[k] ? meta(k) : null;
    },
    async get(k: string, opts: { range: Headers }) {
      if (!files[k]) return null;
      const m = /bytes=(\d+)-(\d*)/.exec(opts.range.get("Range") ?? "");
      const data = files[k];
      if (!m) return { ...meta(k), body: new Blob([data]).stream() };
      const start = Number(m[1]);
      const end = m[2] ? Number(m[2]) : data.length - 1;
      return {
        ...meta(k),
        range: { offset: start, length: end - start + 1 },
        body: new Blob([data.slice(start, end + 1)]).stream(),
      };
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
globalThis.fetch = (async (url: string) => {
  nasCalls.push(url);
  return new Response("from-nas", { status: 200 });
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

test("NAS hit on episode media leaves a promote marker; R2 hit and feed.xml don't", async () => {
  const e = env({ [`feeds/${TOK}/EP02-0123abcd.mp3`]: new Uint8Array([1]) });
  pending = [];
  await call(e, MP3); // miss -> NAS
  await call(e, `/feeds/${TOK}/EP02-0123abcd.mp3`); // R2 hit
  await call(e, `/feeds/${TOK}/feed.xml`); // miss, but not evictable media
  await Promise.all(pending);
  const markers = Object.keys(e.BUCKET.files).filter((k) => k.startsWith("_promote/"));
  assert.deepEqual(markers, [`_promote${MP3}`]);
});
