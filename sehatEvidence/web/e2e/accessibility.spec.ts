import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

/** Runs axe-core against the pages that matter and checks the
 * AgentActivityPanel's focus trap / Escape-close by hand (axe doesn't
 * verify keyboard *behavior*, only static a11y-tree properties). */

test.describe("accessibility", () => {
  test("landing page has no serious/critical axe violations", async ({ page }) => {
    await page.goto("/");
    const results = await new AxeBuilder({ page }).analyze();
    const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious, JSON.stringify(serious, null, 2)).toEqual([]);
  });

  test("ask page (with results rendered) has no serious/critical axe violations", async ({ page }) => {
    await page.goto("/#/ask");
    await page.getByPlaceholder(/Does metformin/).fill("Accessibility scan question");
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.getByText("Answer", { exact: true })).toBeVisible({ timeout: 15000 });
    // let the panel finish its auto-close so it isn't scanned mid-transition
    await page.waitForTimeout(700);

    const results = await new AxeBuilder({ page }).analyze();
    const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious, JSON.stringify(serious, null, 2)).toEqual([]);
  });

  test("history page has no serious/critical axe violations", async ({ page }) => {
    await page.goto("/#/history");
    const results = await new AxeBuilder({ page }).analyze();
    const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious, JSON.stringify(serious, null, 2)).toEqual([]);
  });

  test("agent activity panel: focus is trapped and Escape closes it", async ({ page }) => {
    await page.goto("/#/ask");
    await page.getByPlaceholder(/Does metformin/).fill("Focus trap test question");
    await page.getByRole("button", { name: "Ask" }).click();

    const panel = page.getByRole("dialog", { name: "Agent activity" });
    await expect(panel).toBeVisible();

    // Focus should have moved into the panel, not stayed on the page body.
    const active = await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') != null);
    expect(active).toBe(true);

    // Escape closes it.
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
  });
});
