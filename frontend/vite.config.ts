import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const target = process.env.API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    allowedHosts: true,
    proxy: {
      '/api': { target, rewrite: (p) => p.replace(/^\/api/, ''), xfwd: true },
    },
  },
})
