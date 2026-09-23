/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Where the session document lives.
   *
   * Unset (the default) means `/session.json`, served by the local Python
   * server. On Cloudflare Pages this points at a recorded session committed as
   * a static file, so a visitor sees a real verdict with no setup.
   */
  readonly VITE_SESSION_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
