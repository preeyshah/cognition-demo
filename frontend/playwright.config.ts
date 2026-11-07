import { defineConfig } from '@playwright/test'

// E2E runs against a deployed stack — docker compose (`:8080`) or `npm run dev` + backend (`:5173`).
// Point E2E_BASE_URL at whichever is up; the stack must be seeded (ENVIRONMENT=dev).
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  workers: 1, // specs share seeded demo data; keep them serial
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8080',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
})
