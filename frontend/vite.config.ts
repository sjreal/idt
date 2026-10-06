import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const apiTarget = process.env.API_PROXY_TARGET ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(fileURLToPath(new URL('.', import.meta.url)), 'src'),
    },
  },
  server: {
    host: '0.0.0.0',
    proxy: {
      '/api': apiTarget,
      '/v1': apiTarget,
      '/health': apiTarget,
    },
  },
})
