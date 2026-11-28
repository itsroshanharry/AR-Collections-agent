import { expect, test, type Page } from "@playwright/test";

// Browser smoke tests: every page renders, nothing errors in the console or on the
// server, and the access rules hold. They do not re-test the workflow's logic (pytest
// does that); they prove the built console, the API and the database work together.

const OPERATOR_KEY = process.env.SMOKE_OPERATOR_KEY;
const ADMIN_KEY = process.env.SMOKE_ADMIN_KEY;

const PAGES = [
  { path: "/app/", heading: "Overview" },
  { path: "/app/calls", heading: "Call queue" },
  { path: "/app/customers", heading: "Customers" },
  { path: "/app/tasks", heading: "Work queues" },
  { path: "/app/cash", heading: "Cash application" },
  { path: "/app/experiments", heading: "Prompt experiments" },
];

type Me = { auth_enabled: boolean };

/** Fail the test on any console error, uncaught exception or 5xx response. */
function watchForErrors(page: Page): string[] {
  const problems: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") problems.push(`console: ${msg.text()}`);
  });
  page.on("pageerror", (err) => problems.push(`exception: ${err.message}`));
  page.on("response", (res) => {
    if (res.status() >= 500) problems.push(`${res.status()} ${res.request().method()} ${res.url()}`);
  });
  return problems;
}

async function heading(page: Page, name: string | RegExp) {
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
}

/** Open the first link matching `href` on the page, if there is one. Waits for the
 *  page's data first: headings render before it arrives, so an early look would find
 *  no links and quietly skip the page. */
async function followFirst(page: Page, href: string): Promise<boolean> {
  await page.waitForLoadState("networkidle");
  const link = page.locator(`main a[href*="${href}"]`).first();
  if ((await link.count()) === 0) {
    test.info().annotations.push({ type: "skipped page", description: `no ${href} link to follow` });
    return false;
  }
  await link.click();
  return true;
}

async function signIn(page: Page, key: string, role: string) {
  await page.goto("/app/");
  await heading(page, "Overview");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByLabel("API key").fill(key);
  await page.locator("dialog").getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText(`Signed in as ${role}.`)).toBeVisible();
  await expect(page.locator("dialog[open]")).toHaveCount(0);
}

function toast(page: Page, text: RegExp) {
  return expect(page.getByText(text).first()).toBeVisible();
}

test("a visitor can open every page", async ({ page, request }) => {
  const problems = watchForErrors(page);
  const me: Me = await (await request.get("/auth/me")).json();

  for (const { path, heading: name } of PAGES) {
    await page.goto(path);
    await heading(page, name);
    if (me.auth_enabled) {
      await expect(page.getByText("This is a read-only demo")).toBeVisible();
    }
  }

  // The detail pages, wherever the data has them.
  await page.goto("/app/customers");
  await heading(page, "Customers");
  if (await followFirst(page, "/app/customers/")) {
    await expect(page.getByText("Invoices").first()).toBeVisible();
    if (await followFirst(page, "/app/invoices/")) await heading(page, /INV-/);
  }
  await page.goto("/app/experiments");
  await heading(page, "Prompt experiments");
  if (await followFirst(page, "/app/experiments/")) {
    await expect(page.getByText(/Scenarios \(\d+\)/)).toBeVisible();
    if (await followFirst(page, "/scenarios/")) await expect(page.getByText("The scripted customer")).toBeVisible();
  }

  await page.goto("/app/does-not-exist");
  await heading(page, "Page not found");

  if (me.auth_enabled) {
    // Actions are visibly off for a visitor, and say why.
    await page.goto("/app/cash");
    await expect(page.getByRole("button", { name: "Simulate bank feed" })).toBeDisabled();
    await expect(page.locator("span[title^='Needs the operator role']").first()).toBeAttached();
  }
  expect(problems).toEqual([]);
});

test("an operator can load the ledger and work it", async ({ page }) => {
  test.skip(!OPERATOR_KEY, "Set SMOKE_OPERATOR_KEY to run the signed-in tests");
  const problems = watchForErrors(page);
  await signIn(page, OPERATOR_KEY!, "operator");

  const load = page.getByRole("button", { name: "Load ledger", exact: true });
  if (await load.isVisible()) {
    await load.click();
    await toast(page, /Loaded \d+ invoices into the workflow/);
  }
  // An operator may not reset the demo.
  await expect(page.getByRole("button", { name: "Reset demo" })).toBeDisabled();

  await page.getByRole("button", { name: "Run scheduler pass" }).click();
  await toast(page, /Scheduler pass: \d+ emails/);

  await page.goto("/app/calls");
  await heading(page, "Call queue");
  if (await followFirst(page, "/app/invoices/")) {
    await heading(page, /INV-/);
    // Every timeline event names who did it ("Synced 28 Nov, 08:34 · admin").
    await expect(page.getByText(/\d{2}:\d{2} · [a-z-]+/).first()).toBeVisible();
  }

  await page.goto("/app/cash");
  await heading(page, "Cash application");
  await page.getByRole("button", { name: "Simulate bank feed" }).click();
  await toast(page, /\d+ credits: \d+ matched/);

  await page.goto("/app/experiments");
  await heading(page, "Prompt experiments");
  await page.getByRole("button", { name: "Sync from agent" }).click();
  await toast(page, /prompt version\(s\) from the agent/);
  await expect(page.getByText("collections_formal_v1").first()).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("an admin can reset the demo, and acting as a viewer switches actions off", async ({ page }) => {
  test.skip(!ADMIN_KEY, "Set SMOKE_ADMIN_KEY to run the admin test");
  const problems = watchForErrors(page);
  await signIn(page, ADMIN_KEY!, "admin");

  await page.getByLabel("Act as role").selectOption("viewer");
  await expect(page.getByText("You are acting as a viewer")).toBeVisible();
  await page.getByLabel("Act as role").selectOption("admin");
  await expect(page.getByText("You are acting as a viewer")).toHaveCount(0);

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Reset demo" }).click();
  await toast(page, /Demo reset: \d+ invoices reloaded/);
  expect(problems).toEqual([]);
});
