import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
const seed = JSON.parse(
  readFileSync(
    process.env.E2E_SEED_FILE || "/private/tmp/gas-e2e.json",
    "utf8",
  ),
);
async function login(page: Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Everything, in view." }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Governance", exact: true }).click();
}
test("operator drafts and previews; administrator publishes and rolls back", async ({
  page,
}) => {
  await login(page, seed.email, seed.password);
  const builder = page.getByRole("region", { name: "Policy builder" });
  await builder
    .getByLabel("Policy workflow")
    .selectOption(seed.policy_workflow_id);
  await expect(
    builder.getByText("Active revision: 1", { exact: true }),
  ).toBeVisible();
  await builder.getByLabel("Loop request cap per minute").fill("180");
  await builder.getByLabel("Database outage behavior").selectOption("open");
  await expect(builder.getByLabel("Email", { exact: true })).toBeChecked();
  await builder.getByRole("button", { name: "Add rule", exact: true }).click();
  await builder.getByLabel("Rule ID", { exact: true }).fill("send-large");
  await builder
    .getByRole("button", { name: "Add condition", exact: true })
    .click();
  await builder
    .getByLabel("Sample action JSON")
    .fill('{"amount":150,"recipient":"person@example.test"}');
  await builder
    .getByRole("button", { name: "Preview action", exact: true })
    .click();
  await expect(builder.getByLabel("Policy preview result")).toContainText(
    '"decision": "pending"',
  );
  await builder
    .getByLabel("Change reason", { exact: true })
    .fill("Require review above threshold");
  await builder
    .getByRole("button", { name: "Save policy draft", exact: true })
    .click();
  await expect(builder.getByRole("status")).toContainText(
    "Operation completed",
  );
  await builder.getByLabel("Compare or load revision").selectOption("2");
  await expect(
    builder.getByRole("button", { name: "Publish revision 2", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await login(page, seed.admin_email, seed.admin_password);
  await builder
    .getByLabel("Policy workflow")
    .selectOption(seed.policy_workflow_id);
  await builder.getByLabel("Compare or load revision").selectOption("2");
  await builder
    .getByLabel("Change reason", { exact: true })
    .fill("Administrator reviewed preview");
  await builder
    .getByRole("button", { name: "Publish revision 2", exact: true })
    .click();
  await expect(
    builder.getByText("Active revision: 2", { exact: true }),
  ).toBeVisible();
  await builder.getByLabel("Compare or load revision").selectOption("1");
  await builder
    .getByRole("button", {
      name: "Publish rollback as new revision",
      exact: true,
    })
    .click();
  await expect(
    builder.getByText("Active revision: 3", { exact: true }),
  ).toBeVisible();
  await builder.screenshot({ path: "test-results/phase3-policy-builder.png" });
});
test("configure shared monthly allowance and stage, activate, retire credentials", async ({
  page,
}) => {
  await login(page, seed.email, seed.password);
  const budgets = page.getByRole("region", { name: "Budget controls" });
  await budgets.getByLabel("Budget owner").selectOption(seed.client_id);
  await budgets.getByLabel("Monthly cap (USD)").fill("50");
  await budgets
    .getByLabel("Reason", { exact: true })
    .fill("Client-wide monthly allowance");
  await expect(
    budgets.getByLabel("Repeat monthly from next UTC month"),
  ).toBeChecked();
  await budgets
    .getByRole("button", { name: "Save budget", exact: true })
    .click();
  await expect(budgets.getByRole("status")).toContainText("Change saved");
  await expect(
    budgets.getByRole("cell", { name: "$50.00", exact: true }).first(),
  ).toBeVisible();
  const credentials = page.getByRole("region", { name: "Credential controls" });
  await credentials
    .getByLabel("Credential client")
    .selectOption(seed.client_id);
  await credentials
    .getByRole("combobox", { name: "Provider", exact: true })
    .selectOption("gemini");
  await credentials.getByLabel("Slot label").fill("e2e-rotation");
  await credentials
    .getByLabel("New provider key")
    .fill("mock-first-credential");
  await credentials
    .getByLabel("Credential change reason")
    .fill("Controlled mock rotation");
  await credentials
    .getByRole("button", { name: "Create credential", exact: true })
    .click();
  await expect(credentials.getByRole("status")).toContainText(
    "metadata updated",
  );
  const list = await (
    await page.request.get("/api/admin/provider-credentials")
  ).json();
  const slot = list.find((s: { label: string }) => s.label === "e2e-rotation");
  await credentials.getByLabel("Credential slot").selectOption(slot.id);
  await credentials
    .getByLabel("New provider key")
    .fill("mock-second-credential");
  await credentials
    .getByRole("button", { name: "Stage replacement", exact: true })
    .click();
  await expect(
    credentials.getByRole("button", {
      name: "Activate version 2",
      exact: true,
    }),
  ).toBeVisible();
  await credentials
    .getByRole("button", { name: "Activate version 2", exact: true })
    .click();
  const article = credentials
    .getByRole("heading", { name: /e2e-rotation/ })
    .locator("..");
  await expect(article).toContainText("Active version 2");
  await credentials
    .getByRole("button", { name: "Retire version 2", exact: true })
    .click();
  await expect(article).toContainText("No active version");
  await expect(credentials.getByLabel("New provider key")).toHaveValue("");
  await page.setViewportSize({ width: 390, height: 844 });
  await credentials.screenshot({
    path: "test-results/phase3-controls-mobile.png",
  });
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
});
