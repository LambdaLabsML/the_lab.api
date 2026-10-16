import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
  plugins: [preact()],
  css: {
    preprocessorOptions: {
      scss: {
        api: "modern-compiler",
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/openapi.json": "http://localhost:8000",
    },
  },
  build: {
    outDir: "../the_lab/static",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Stable vendor chunk: app deploys (several a day) no longer
        // invalidate the browser's copy of preact/signals.
        manualChunks: {
          vendor: ["preact", "preact/hooks", "@preact/signals"],
        },
      },
    },
  },
});
