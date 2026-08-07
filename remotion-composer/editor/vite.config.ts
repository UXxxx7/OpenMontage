import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// root=this dir; fs.allow reaches two levels up because both
// XiaojinEditorial.tsx and App.tsx import ../../contracts/render_props.schema.json
// (repo root), outside this Vite root. Confirmed necessary and sufficient in
// the Stage-0 spike (see plan: wiggly-cooking-eich.md).
export default defineConfig({
  root: __dirname,
  // server/index.js serves this build's assets at /editor/assets and the
  // shell at /editor/:jobId (not at the domain root) -- without `base` here,
  // Vite emits root-absolute asset URLs ("/assets/...") that 404 once the
  // page is actually served from under /editor/, since the browser resolves
  // them against the origin, not the page path. Confirmed via a live browser
  // load: net::ERR_ABORTED on the script tag until this was set.
  base: "/editor/",
  plugins: [react()],
  server: {
    fs: { allow: [resolve(__dirname, "../..")] },
    port: 5173,
    // Binds all interfaces, not just localhost — the production gateway
    // (server/index.js) already does this, but the dev server didn't,
    // making it unreachable from a phone on the same Wi-Fi and needlessly
    // painful to iterate on real-device touch/pinch behavior (Phase 6, F4).
    host: true,
  },
  optimizeDeps: {
    include: ["ajv/dist/2020"],
  },
  build: {
    outDir: resolve(__dirname, "../editor-dist"),
    emptyOutDir: true,
  },
});
