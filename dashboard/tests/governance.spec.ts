import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const seed = JSON.parse(
  readFileSync(
    process.env.E2E_SEED_FILE || "/private/tmp/gas-e2e.json",
    "utf8",
  ),
);
test("create key, call native gateway, view spend, and kill workflow", async ({
  page,
  request,
}) => {
  await page.goto("/login");
  await page.getByLabel("Email", { exact: true }).fill(seed.email);
  await page.getByLabel("Password", { exact: true }).fill(seed.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Everything, in view." }),
  ).toBeVisible();
  const row = page.getByTestId("workflow-" + seed.workflow_id);
  await row.getByRole("button", { name: "Create key", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  const key = (await page.getByTestId("new-key").textContent())!;
  await page.getByRole("button", { name: "Close dialog" }).click();
  const response = await request.post(
    (process.env.E2E_GATEWAY_URL || "http://localhost:8000") +
      "/v1/chat/completions",
    {
      headers: { Authorization: "Bearer " + key },
      data: {
        model: "gpt-mock",
        max_tokens: 16,
        messages: [{ role: "user", content: "hello" }],
      },
    },
  );
  expect(response.status()).toBe(200);
  await page.getByRole("button", { name: "Refresh dashboard" }).click();
  await expect(row).toContainText("$0.000015");
  await expect(page.getByTestId("monthly-spend")).toContainText("$0.000015");
  await page.screenshot({
    path: "test-results/dashboard-overview.png",
    fullPage: true,
  });
  await row.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(row.getByText("Paused", { exact: true })).toBeVisible();
  await expect
    .poll(
      async () =>
        (
          await request.post(
            (process.env.E2E_GATEWAY_URL || "http://localhost:8000") +
              "/v1/chat/completions",
            {
              headers: { Authorization: "Bearer " + key },
              data: { model: "gpt-mock", max_tokens: 16 },
            },
          )
        ).status(),
      { timeout: 8000 },
    )
    .toBe(503);
  await page
    .getByRole("button", { name: "Reconciliation", exact: true })
    .click();
  await expect(
    page.getByText("No reporting jobs yet", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Reports", exact: true }).click();
  await page.getByLabel("Minutes saved", { exact: true }).fill("123");
  await page
    .getByLabel("Evidence note", { exact: true })
    .fill("Estimated from two hours of baseline work and three minutes saved.");
  await page.getByRole("button", { name: "Save savings", exact: true }).click();
  await expect(
    page.getByText("Savings evidence saved.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("2.05", { exact: true })).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: "Export CSV", exact: true }).click(),
  ]);
  await download.saveAs("test-results/monthly-report.csv");
  const [printable] = await Promise.all([
    page.waitForEvent("popup"),
    page.getByRole("link", { name: "Printable HTML", exact: true }).click(),
  ]);
  await expect(
    printable.getByText("2.050000 hours (estimate)", { exact: true }),
  ).toBeVisible();
  await printable.screenshot({
    path: "test-results/monthly-report.png",
    fullPage: true,
  });
  await printable.close();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expect(page.getByTestId("monthly-spend")).toBeVisible();
  await page.screenshot({
    path: "test-results/dashboard-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Add account", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome back", exact: true }),
  ).toBeVisible();
});
test("reject cross-origin cookie mutations", async ({ request }) => {
  const response = await request.post("/api/auth/login", {
    headers: { Origin: "https://foreign.example" },
    data: { email: seed.email, password: seed.password },
  });
  expect(response.status()).toBe(403);
});
