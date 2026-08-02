import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // Vite 5+ rejects requests with an unrecognized Host header by
    // default (blocks DNS-rebinding attacks) — necessary when accessed
    // through an ngrok tunnel, whose hostname isn't localhost. ".ngrok-free.app"/
    // ".ngrok-free.dev" covers ngrok's free-tier random subdomains generally,
    // so a rotated tunnel URL doesn't need this list updated again.
    allowedHosts: ['.ngrok-free.app', '.ngrok-free.dev'],
    proxy: {
      // Forward all /api/* requests to the FastAPI backend. No path
      // rewrite: every backend router is itself mounted under /api/...
      // (see src/main.py's include_router prefix="/api/..." calls), so
      // stripping /api here would 404 against the real route paths —
      // confirmed live, the previous rewrite: path.replace(/^\/api/, '')
      // was never actually exercised (the frontend calls the backend via
      // an absolute VITE_API_URL, bypassing this proxy entirely) and
      // would have been broken the moment anything relied on it.
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
})

