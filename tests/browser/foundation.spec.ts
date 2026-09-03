import { expect, test } from "@playwright/test";

test("Fresh foundation renders and hydrates without overflow", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "QBot4K" }))
    .toBeVisible();
  await page.getByRole("button", { name: "Check" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  const overflows = await page.evaluate(() =>
    document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  expect(overflows).toBe(false);
});
