import { expect, test } from "@playwright/test";

test(
  "front page displays Stripe subscription pricing",
  async ({ page }, testInfo) => {
    await page.goto("/");
    const pricing = page.getByRole("region", { name: "Stripe pricing" });
    await expect(pricing.getByRole("heading", { name: "Monthly", exact: true }))
      .toBeVisible();
    await expect(pricing.getByRole("heading", { name: "Annual", exact: true }))
      .toBeVisible();
    await expect(pricing.getByText("$9.99", { exact: true })).toBeVisible();
    await expect(pricing.getByText("$99.99", { exact: true })).toBeVisible();
    await expect(pricing).toContainText(
      "USD per organization. Includes 1 community, with 1 Twitch channel and up to 3 Discord servers.",
    );
    await expect(pricing).toContainText("Save $19.89 a year");
    await expect(pricing.getByRole("link", { name: "View billing" }))
      .toHaveAttribute("href", "/billing");
    expect(
      await page.evaluate(() =>
        document.documentElement.scrollWidth <= globalThis.innerWidth
      ),
    ).toBe(true);
    await pricing.screenshot({ path: testInfo.outputPath("pricing.png") });
  },
);

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
