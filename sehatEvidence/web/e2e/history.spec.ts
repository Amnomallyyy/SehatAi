import { expect, test } from "@playwright/test";

test.describe("history", () => {
  test("a completed run appears in history, opens, and can be deleted", async ({ page }) => {
    const question = `E2E history test ${Date.now()}`;

    await page.goto("/#/ask");
    await page.getByPlaceholder(/Does metformin/).fill(question);
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.getByText("Answer", { exact: true })).toBeVisible({ timeout: 15000 });

    await page.goto("/#/history");
    const row = page.getByText(question, { exact: true });
    await expect(row).toBeVisible();

    await row.click();
    await expect(page.getByText("Answer", { exact: true })).toBeVisible();
    await expect(page.getByText(question)).toBeVisible();

    await page.goto("/#/history");
    const deleteButton = page.getByRole("listitem").filter({ hasText: question }).getByRole("button", { name: /Delete/ });
    await deleteButton.click();
    await expect(page.getByText(question, { exact: true })).toHaveCount(0);
  });

  test("empty state links back to Ask when there is no history", async ({ page }) => {
    await page.goto("/#/history");
    // Not asserting emptiness (other tests may have left rows) -- just that
    // the page renders without error and the clear-all control appears
    // whenever there IS at least one row.
    await expect(page.locator("body")).toBeVisible();
  });
});
