import { bindings, defineConfig, triggers } from "cf/config";

export default defineConfig({
  worker: {
    name: "podcast-feed",
    entrypoint: "./src/index.ts",
    compatibilityDate: "2026-10-12",
    compatibilityFlags: ["nodejs_compat"],
    // Parallel stand-up: verify on workers.dev first; the custom domain comes at cutover.
    workersDev: true, // kept: direct test URL that bypasses the custom domain
    previewUrls: false,
    // A zone route, not a custom domain: the existing proxied CNAME to the tunnel stays,
    // so cutover and rollback are both instant (rollback = drop this route + redeploy).
    triggers: [triggers.fetch({ pattern: "podcast.audichuang.app/*", zone: "audichuang.app" })],
    observability: { enabled: true, headSamplingRate: 0.1 },
    env: {
      BUCKET: bindings.r2({ name: "podcast-feeds" }),
      // Fallback for anything not in R2: the NAS Caddy on its own tunnel hostname. Must
      // never be podcast.audichuang.app — that hostname is this Worker (fetch loop).
      NAS_ORIGIN: bindings.text("https://podcast-nas.audichuang.app"),
      // Same value as the NAS uploader's token (Komodo PODCAST_UPLOAD_TOKEN / Doppler).
      UPLOAD_TOKEN: bindings.secret(),
    },
  },
});
