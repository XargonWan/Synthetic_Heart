import { fileURLToPath } from 'node:url'

import vue from '@vitejs/plugin-vue'
import unocss from 'unocss/vite'
import { defineConfig } from 'vite'

// Backend origin for the dev proxy. TLS is on by default in SyntH with a
// self-signed cert; `secure: false` lets Node's proxy accept it so the
// browser only ever talks to the plain-HTTP Vite origin.
const backend = process.env.SYNTH_BACKEND_ORIGIN ?? 'https://localhost:8080'

const proxyHttp = { target: backend, secure: false, changeOrigin: true }
const proxyWs = { ...proxyHttp, ws: true }

export default defineConfig({
  // Served by FastAPI at /stage in production (see core/webui.py).
  base: '/stage/',
  plugins: [vue(), unocss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': proxyWs,
      '/ws': proxyWs,
      '/skins': proxyHttp,
      '/uploads': proxyHttp,
      '/avatars': proxyHttp,
    },
  },
  build: {
    target: 'es2022',
  },
})
