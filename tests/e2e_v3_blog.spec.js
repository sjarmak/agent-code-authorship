const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const path = require("path");
const { pathToFileURL } = require("url");

const documentPath =
  process.env.BLOG_HTML ||
  path.resolve("results/agent-code-authorship-sourcegraph.html");
const documentUrl = pathToFileURL(documentPath).href;

const viewports = [
  { name: "mobile", width: 375, height: 812 },
  { name: "tablet", width: 768, height: 900 },
  { name: "desktop", width: 1440, height: 1000 },
];

for (const viewport of viewports) {
  test(`${viewport.name} document has no page overflow or runtime errors`, async ({
    page,
  }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto(documentUrl);

    await expect(page).toHaveTitle(/How much of open source is agent-written/);
    await expect(page.locator("h1")).toContainText("how much survives");
    await expect(page.locator("figure")).toHaveCount(4);
    await expect(page.locator("figcaption")).toHaveCount(4);
    await expect(page.locator("script")).toHaveCount(0);
    expect(errors).toEqual([]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `/tmp/aca-blog-${viewport.name}.png`,
    });
  });
}

test("section navigation reaches every evidence block", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(documentUrl);

  for (const id of [
    "footprint",
    "harnesses",
    "languages",
    "age",
    "write-vs-head",
    "reverts",
    "methods",
    "artifacts",
  ]) {
    await page
      .locator(`nav[aria-label="Study navigation"] .rail-index a[href="#${id}"]`)
      .click();
    await expect(page.locator(`#${id}`)).toBeInViewport();
  }
});

test("key evidence sections render on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(documentUrl);

  await page.locator("#footprint").screenshot({
    path: "/tmp/aca-footprint-section-mobile.png",
  });
  await page.locator("#harnesses").screenshot({
    path: "/tmp/aca-harness-section-mobile.png",
  });
  await page.locator("#languages").screenshot({
    path: "/tmp/aca-language-section-mobile.png",
  });
  await page.locator("#age").screenshot({
    path: "/tmp/aca-age-section-mobile.png",
  });
  await page.locator("#reverts").screenshot({
    path: "/tmp/aca-reverts-section-mobile.png",
  });
});

test("document has no automated WCAG A or AA violations", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(documentUrl);

  const audit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();

  expect(audit.violations).toEqual([]);
});
