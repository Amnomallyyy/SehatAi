import { expect, test } from "@playwright/test";

test.describe("ask flow (mock mode)", () => {
  test("landing page renders the pipeline explainer", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "That funnel is the product." })).toBeVisible();
    await expect(page.getByText("Strategist").first()).toBeVisible();
    await expect(page.getByText("Red Team").first()).toBeVisible();
  });

  test("submitting a question renders the full results tree", async ({ page }) => {
    await page.goto("/#/ask");
    await page.getByPlaceholder(/Does metformin/).fill("E2E ask-flow test question");
    await page.getByRole("button", { name: "Ask" }).click();

    // Results should appear (mock mode resolves fast; give it real room anyway).
    await expect(page.getByText("Answer", { exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByText("Verification funnel", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: /Evidence pool/ })).toBeVisible();

    // Claim tabs: Kept/Flagged/Deleted counts from the SGLT2 mock fixture.
    await expect(page.getByRole("button", { name: /Flagged/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /Deleted/ })).toBeVisible();

    // The retracted record must be visibly flagged, not just present.
    await expect(page.getByText(/RETRACTED:/)).toBeVisible();
    await expect(page.getByText(/retracted.*pubmed|pubmed.*retracted/i).first()).toBeVisible();
  });

  test("cache: asking the same question twice returns a cache hit the second time", async ({ page }) => {
    const question = `E2E cache test ${Date.now()}`;
    await page.goto("/#/ask");
    await page.getByPlaceholder(/Does metformin/).fill(question);
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.getByText("Answer", { exact: true })).toBeVisible({ timeout: 15000 });

    await page.reload();
    await page.getByPlaceholder(/Does metformin/).fill(question);
    await page.getByRole("button", { name: "Ask" }).click();
    // The panel should show the cache-hit state at some point during the run.
    await expect(page.getByText(/Served from a previous run/)).toBeVisible({ timeout: 10000 });
  });

  test("disclaimer appears on more than one surface", async ({ page }) => {
    await page.goto("/#/ask");
    const disclaimerCountBefore = await page.getByText(/not a medical device/i).count();
    expect(disclaimerCountBefore).toBeGreaterThanOrEqual(1);

    await page.getByPlaceholder(/Does metformin/).fill("E2E disclaimer repetition test");
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.getByText("Answer", { exact: true })).toBeVisible({ timeout: 15000 });

    const disclaimerCountAfter = await page.getByText(/not a medical device/i).count();
    expect(disclaimerCountAfter).toBeGreaterThanOrEqual(2);
  });
});
