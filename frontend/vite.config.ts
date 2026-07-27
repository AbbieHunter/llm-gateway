import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// M0: build output goes to the backend's static dir so FastAPI can serve it
// via StaticFiles (single-process deploy, see ARCHITECTURE §4.9). `base: './'`
// keeps asset paths relative for the catch-all SPA mount.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../app/static/dist",
    emptyOutDir: true,
  },
});
