import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.js",
  },
  build: {
    outDir: "../build/frontend",
    emptyOutDir: true,
  },
});
