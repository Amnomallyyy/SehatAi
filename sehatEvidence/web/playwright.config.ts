import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config. Tests run against the PRODUCTION static build served by
 * api/server.py (python -m api.server --mock on :8000) -- the real
 * deployment path, not the Vite dev server -- so a passing suite proves
 * the built artifact actually works, not just source-mode HMR.
 *
 * Requires `npm run build` (in web/) and a mock-mode Python server
 * already running on :8000 before `npx playwright test`.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
