import { expect, test } from "@playwright/test";

test("Fresh foundation renders without overflow", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "QBot4K" }))
    .toBeVisible();
  await expect(page.getByRole("link", { name: "Link Discord" })).toBeVisible();
  await page.getByRole("button", { name: "Check now" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  const overflows = await page.evaluate(() =>
    document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  expect(overflows).toBe(false);
});

test.describe("without JavaScript", () => {
  test.use({ javaScriptEnabled: false });

  test("core status navigation remains available", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Online", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: "View status" }).click();
    await expect(page.locator("body")).toContainText('"status":"ready"');
  });

  test("public legal navigation remains available", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "Privacy" }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: "Privacy policy" }),
    )
      .toBeVisible();
  });
});
