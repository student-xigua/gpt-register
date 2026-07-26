const { test, expect } = require("@playwright/test");

const BASE = process.env.UI_BASE_URL || "http://127.0.0.1:8876";
const SHOTS = process.env.UI_SCREENSHOT_DIR || "/tmp";
test.use({ channel: "chrome" });

async function visibleBodyRows(page) {
  return page.locator("#accountsTable tbody tr").evaluateAll(rows => {
    const viewport = document.querySelector(".table-scroll").getBoundingClientRect();
    return rows.filter(row => {
      const rect = row.getBoundingClientRect();
      return rect.bottom <= viewport.bottom + 1 && rect.top >= viewport.top - 1;
    }).length;
  });
}

for (const viewport of [
  { width: 1366, height: 768, minimumRows: 10 },
  { width: 1440, height: 1000, minimumRows: 15 },
]) {
  test(`accounts workbench ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto(`${BASE}/accounts`);
    const totals = await page.evaluate(async () => {
      const all = await fetch("api/registered?limit=1&filter=all").then(r => r.json());
      const noRt = await fetch("api/registered?limit=1&filter=no_rt").then(r => r.json());
      return { all: all.total, noRt: noRt.total };
    });
    await expect(page.locator("#accountsTable tbody tr")).toHaveCount(Math.min(20, totals.all));
    expect(await visibleBodyRows(page)).toBeGreaterThanOrEqual(viewport.minimumRows);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);

    await page.locator("#tableScroll").evaluate(node => { node.scrollTop = 240; });
    const headerTop = await page.locator("#accountsTable thead th").first().evaluate(
      node => Math.round(node.getBoundingClientRect().top),
    );
    const scrollTop = await page.locator("#tableScroll").evaluate(
      node => Math.round(node.getBoundingClientRect().top),
    );
    expect(Math.abs(headerTop - scrollTop)).toBeLessThanOrEqual(1);
    await page.locator("#tableScroll").evaluate(node => { node.scrollTop = 0; });
    await page.screenshot({
      path: `${SHOTS}/account-management-${viewport.width}x${viewport.height}.png`,
      fullPage: false,
    });

    await page.locator(".row-check").first().check();
    await expect(page.locator("#copyAtBtn")).toBeEnabled();
    const downloadable = page.locator('[data-action="download"]:not([disabled])');
    if (await downloadable.count()) {
      await downloadable.first().locator("xpath=ancestor::tr").locator(".row-check").check();
      await expect(page.locator("#downloadBtn")).toBeEnabled();
    } else {
      await expect(page.locator("#downloadBtn")).toBeDisabled();
    }
    await page.locator("#pageSize").selectOption("50");
    await expect(page.locator("#accountsTable tbody tr")).toHaveCount(Math.min(50, totals.all));
    await page.locator('[data-filter="no_rt"]').click();
    await expect(page.locator("#accountsTable tbody tr")).toHaveCount(
      totals.noRt ? Math.min(50, totals.noRt) : 1,
    );
  });
}

test("pool workbench navigation and density", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto(`${BASE}/pool`);
  const counts = await page.evaluate(async () => {
    const all = await fetch("api/accounts?limit=1").then(r => r.json());
    const failed = await fetch("api/accounts?status=failed&limit=1").then(r => r.json());
    return { all: all.total, failed: failed.total };
  });
  await expect(page.locator("#poolTable tbody tr")).toHaveCount(Math.min(50, counts.all));
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1366);
  await page.screenshot({
    path: `${SHOTS}/pool-management-1366x768.png`,
    fullPage: false,
  });
  await page.locator("#toggleImportBtn").click();
  await expect(page.locator("#importPanel")).toBeVisible();
  await page.locator("#statusFilter").selectOption("failed");
  await expect(page.locator("#poolTable tbody tr")).toHaveCount(
    counts.failed ? Math.min(50, counts.failed) : 1,
  );
  await expect(page.locator('a[href="accounts"]')).toBeVisible();
  await expect(page.locator(".console-link")).toHaveAttribute("href", "./");
});
