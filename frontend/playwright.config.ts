import { defineConfig, devices } from "@playwright/test";

// Browser smoke tests for the console, run against a server that is already up: the
// Docker image in CI, a local `uvicorn` in development, or the live Render deploy.
//
//   SMOKE_BASE_URL       where the app is (default http://127.0.0.1:8000)
//   SMOKE_OPERATOR_KEY   enables the tests that sign in and change data
//   SMOKE_ADMIN_KEY      enables the admin test (demo reset)
//
// With no keys, only the read-only visitor test runs, which is what is safe to point at
// the public deploy.
const baseURL = process.env.SMOKE_BASE_URL ?? "http://127.0.0.1:8000";
const remote = !/127\.0\.0\.1|localhost/.test(baseURL);

export default defineConfig({
  testDir: "e2e",
  // The tests share one server and its data, and the signed-in ones change it.
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  // A free Render instance takes up to a minute to wake from sleep.
  timeout: remote ? 120_000 : 45_000,
  expect: { timeout: remote ? 30_000 : 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
