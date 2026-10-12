import { bindings, defineConfig } from "cf/config";

export default defineConfig({
  worker: {
    name: "podcast-feed",
    entrypoint: "./src/index.ts",
    compatibilityDate: "2026-10-12",
    compatibilityFlags: ["nodejs_compat"],
    // Parallel stand-up: verify on workers.dev first; the custom domain comes at cutover.
    workersDev: true,
    previewUrls: false,
    observability: { enabled: true, headSamplingRate: 0.1 },
    env: {
      BUCKET: bindings.r2({ name: "podcast-feeds" }),
      // Fallback for anything not in R2. Until cutover this is the live Caddy hostname;
      // at cutover it moves to a separate tunnel hostname (the Worker takes this one).
      NAS_ORIGIN: bindings.text("https://podcast.audichuang.app"),
      // Same value as the NAS uploader's token (Komodo PODCAST_UPLOAD_TOKEN / Doppler).
      UPLOAD_TOKEN: bindings.secret(),
    },
  },
});
