"use client";
import { useCallback, useEffect, useState } from "react";
import {
  controlApi,
  micro,
  usd,
  type ControlClient,
  type ControlWorkflow,
} from "./controlApi";
type Bucket = {
  id: string;
  client_id: string;
  period: string;
  cap_micro: number | null;
  spent_micro: number;
  reserved_micro: number;
  headroom_micro: number | null;
  revision: number;
};
type Spend = {
  workflows: {
    id: string;
    budget_id: string | null;
    budget_revision: number | null;
    cap_micro: number | null;
    spent_micro: number;
    reserved_micro: number;
  }[];
};
type Template = {
  id: string;
  scope: string;
  scope_id: string;
  revision: number;
  enabled: boolean;
  cap_micro: number;
  effective_period: string;
};
type Stop = {
  id: string;
  workflow_id: string;
  external_run_id: string;
  reason: string;
  revision: number;
  cost_micro: number;
  step_count: number;
};
export default function BudgetControls({
  clients,
  workflows,
  period,
  writable,
}: {
  clients: ControlClient[];
  workflows: ControlWorkflow[];
  period: string;
  writable: boolean;
}) {
  const [buckets, setBuckets] = useState<Bucket[]>([]),
    [spend, setSpend] = useState<Spend>({ workflows: [] });
  const [templates, setTemplates] = useState<Template[]>([]),
    [stops, setStops] = useState<Stop[]>([]);
  const [scope, setScope] = useState("client"),
    [identity, setIdentity] = useState(""),
    [amount, setAmount] = useState("");
  const [recurring, setRecurring] = useState(true),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    const [b, s, t, r] = await Promise.all([
      controlApi<Bucket[]>(`client-budgets?period=${period}`),
      controlApi<Spend>(`spend?period=${period}`),
      controlApi<Template[]>("budget-templates"),
      controlApi<Stop[]>("run-stops"),
    ]);
    setBuckets(b);
    setSpend(s);
    setTemplates(t);
    setStops(r);
  }, [period]);
  useEffect(() => {
    load().catch((e) => setError(String(e.message)));
  }, [load]);
  const resources = scope === "client" ? clients : workflows;
  async function act(operation: () => Promise<void>) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      if (!reason.trim()) throw new Error("Add a reason for this change");
      await operation();
      await load();
      setNotice(
        "Change saved. Existing charges and reservations remain accounted for.",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Change failed");
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (!identity) throw new Error("Select a client or workflow");
    const cap = micro(amount);
    if (scope === "client") {
      const existing = buckets.find((b) => b.client_id === identity);
      await controlApi(
        `clients/${identity}/budget`,
        {
          period,
          cap_micro: cap,
          expected_revision: existing?.revision || 1,
          reason,
        },
        "PUT",
      );
    } else {
      const existing = spend.workflows.find((w) => w.id === identity);
      if (existing?.budget_id)
        await controlApi(
          `budgets/${existing.budget_id}`,
          {
            cap_micro: cap,
            expected_revision: existing.budget_revision,
            reason,
          },
          "PATCH",
        );
      else
        await controlApi("budgets", {
          workflow_id: identity,
          period,
          cap_micro: cap,
        });
    }
    if (recurring) {
      const latest = templates
        .filter((t) => t.scope === scope && t.scope_id === identity)
        .sort((a, b) => b.revision - a.revision)[0];
      await controlApi("budget-templates", {
        scope,
        scope_id: identity,
        cap_micro: cap,
        enabled: true,
        expected_revision: latest?.revision || 0,
        reason,
      });
    }
  }
  return (
    <section className="panel control-panel" aria-label="Budget controls">
      <h2>Budgets and run stops</h2>
      <p>
        Client allowances cover every workflow. Changes include spend and
        reservations already made in the selected UTC month.
      </p>
      {error && (
        <p role="alert" className="control-error">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Client</th>
              <th>Monthly cap</th>
              <th>Spent</th>
              <th>Reserved</th>
              <th>Headroom</th>
            </tr>
          </thead>
          <tbody>
            {clients.map((c) => {
              const b = buckets.find((b) => b.client_id === c.id);
              return (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{usd(b?.cap_micro ?? null)}</td>
                  <td>{usd(b?.spent_micro || 0)}</td>
                  <td>{usd(b?.reserved_micro || 0)}</td>
                  <td>
                    {b?.headroom_micro == null ? "—" : usd(b.headroom_micro)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {writable && (
        <form
          className="control-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void act(save);
          }}
        >
          <label>
            Budget scope
            <select
              value={scope}
              onChange={(e) => {
                setScope(e.target.value);
                setIdentity("");
              }}
            >
              <option value="client">Shared client</option>
              <option value="workflow">Workflow</option>
            </select>
          </label>
          <label>
            Budget owner
            <select
              value={identity}
              onChange={(e) => setIdentity(e.target.value)}
              required
            >
              <option value="">Choose…</option>
              {resources.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Monthly cap (USD)
            <input
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              inputMode="decimal"
              required
            />
          </label>
          <label>
            Reason
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={200}
              required
            />
          </label>
          <label className="control-check">
            <input
              type="checkbox"
              checked={recurring}
              onChange={(e) => setRecurring(e.target.checked)}
            />
            Repeat monthly from next UTC month
          </label>
          <button className="primary small" disabled={busy}>
            Save budget
          </button>
          <p className="muted">
            The current cap changes now. Monthly recurrence starts next month;
            unused allowance does not carry forward.
          </p>
        </form>
      )}
      <h3>Monthly schedules</h3>
      <ul>
        {templates
          .filter(
            (t) =>
              !templates.some(
                (n) =>
                  n.scope === t.scope &&
                  n.scope_id === t.scope_id &&
                  n.revision > t.revision,
              ),
          )
          .map((t) => (
            <li key={t.id}>
              {(t.scope === "client" ? clients : workflows).find(
                (r) => r.id === t.scope_id,
              )?.name || t.scope}{" "}
              · {usd(t.cap_micro)} ·{" "}
              {t.enabled
                ? `from ${t.effective_period} UTC`
                : "renewal disabled"}{" "}
              {writable && t.enabled && (
                <button
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      await controlApi("budget-templates", {
                        scope: t.scope,
                        scope_id: t.scope_id,
                        cap_micro: t.cap_micro,
                        enabled: false,
                        expected_revision: t.revision,
                        reason,
                      });
                    })
                  }
                >
                  Disable future renewal
                </button>
              )}
            </li>
          ))}
      </ul>
      <h3>Stopped runs</h3>
      {!stops.length && <p>No stopped runs.</p>}
      <ul>
        {stops.map((r) => (
          <li key={r.id}>
            {workflows.find((w) => w.id === r.workflow_id)?.name} ·{" "}
            {r.external_run_id} · {r.reason} · {usd(r.cost_micro)} ·{" "}
            {r.step_count} steps{" "}
            {writable && (
              <button
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    await controlApi(
                      `runs/${r.id}/stop`,
                      { active: false, expected_revision: r.revision, reason },
                      "PUT",
                    );
                  })
                }
              >
                Clear run stop
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
