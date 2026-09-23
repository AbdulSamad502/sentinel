import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the built app works from a Cloudflare Pages root, from a
// subpath, and from the local Python server without three different builds.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    // `npm run dev` talks to the local Python server for live session data.
    proxy: { "/session.json": "http://127.0.0.1:8765" },
  },
});
