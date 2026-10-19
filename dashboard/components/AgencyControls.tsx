"use client";
import BudgetControls from "./BudgetControls";
import CredentialControls from "./CredentialControls";
import PolicyBuilder from "./PolicyBuilder";
import type { ControlClient, ControlWorkflow } from "./controlApi";
export default function AgencyControls({
  clients,
  workflows,
  period,
  role,
}: {
  clients: ControlClient[];
  workflows: ControlWorkflow[];
  period: string;
  role: string;
}) {
  const writable = role === "admin" || role === "operator";
  return (
    <div className="agency-controls">
      <BudgetControls
        clients={clients}
        workflows={workflows}
        period={period}
        writable={writable}
      />
      <PolicyBuilder workflows={workflows} role={role} />
      <CredentialControls clients={clients} writable={writable} />
    </div>
  );
}
