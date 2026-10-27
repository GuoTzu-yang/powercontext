import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: resolve(root, "app"),
  publicDir: resolve(root, "app/public"),
  build: {
    outDir: resolve(root, "server/public"),
    emptyOutDir: true,
    target: "es2022",
  },
  worker: { format: "es" },
  server: {
    port: 5273,
    strictPort: true,
    watch: {
      // Vite's worker bundling leaves short-lived temp dirs next to the source file;
      // chokidar on Windows can hit EBUSY watching them and take the dev server down.
      ignored: ["**/.*.tmpdir/**", "**/*.tmp", "**/.seekdb/**"],
    },
    proxy: {
      "/api": { target: "http://127.0.0.1:8791", changeOrigin: true },
    },
  },
  optimizeDeps: {
    exclude: ["@huggingface/transformers"],
  },
});
