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
