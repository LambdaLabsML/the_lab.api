import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
  plugins: [preact()],
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
  },
});
