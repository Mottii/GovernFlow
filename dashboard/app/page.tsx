"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowUpRight,
  Check,
  ChevronDown,
  CircleHelp,
  ClipboardCheck,
  Copy,
  KeyRound,
  FileDown,
  LayoutDashboard,
  LogOut,
  Pause,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  TriangleAlert,
  Workflow as WorkflowIcon,
  X,
} from "lucide-react";

type Me = { email: string; role: string; client_id: string | null };
type Client = { id: string; name: string };
type Workflow = {
  id: string;
  client_id: string;
  name: string;
  risk_class: string;
  policy: Record<string, unknown>;
};
type SpendRow = {
  id: string;
  client_id: string;
  client: string;
  name: string;
  risk_class: string;
  spent_micro: number;
  estimated_micro: number;
  estimated_calls: number;
  recovery_pending: number;
  reserved_micro: number;
  cap_micro: number | null;
};
type Spend = {
  period: string;
  clients: {
    id: string;
    name: string;
    spent_micro: number;
    reserved_micro: number;
  }[];
  workflows: SpendRow[];
  daily: { day: string; cost_micro: number; calls: number }[];
};
type Approval = {
  id: string;
  workflow_id: string;
  action_name: string;
  summary: string;
  status: string;
  expires_at: string;
  approver: string | null;
  consumed: boolean;
};
type Alert = {
  id: string;
  kind: string;
  client_id: string;
  details: Record<string, unknown>;
  created_at: string;
};
type Kill = {
  scope: string;
  scope_id: string;
  active: boolean;
  reason: string | null;
};
type Key = {
  id: string;
  workflow_id: string;
  prefix: string;
  created_at: string;
  revoked_at: string | null;
};
type Reconciliation = {
  jobs: {
    client_id: string;
    provider: string;
    day: string;
    status: string;
    error: string | null;
  }[];
  accounts: {
    client_id: string;
    provider: string;
    external_id: string;
    validated: boolean;
  }[];
  snapshots: {
    client_id: string;
    provider: string;
    day: string;
    kind: string;
    dimension: string;
    gateway: Record<string, number>;
    observed: Record<string, number>;
  }[];
};
type MonthlyReport = {
  period: string;
  client: { id: string; name: string };
  totals: {
    cost_micro: number;
    calls: number;
    estimated_calls: number;
    estimated_cost_micro: number;
    corrected_calls: number;
    recovery_pending: number;
    reserved_micro: number;
  };
  hours_saved: {
    minutes: number;
    hours: string;
    basis: string;
    note: string;
  } | null;
  workflows: {
    id: string | null;
    name: string;
    cost_micro: number;
    calls: number;
    estimated_calls: number;
  }[];
};
const money = (micro: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: micro && micro < 1_000_000 ? 6 : 2,
  }).format(micro / 1_000_000);
const dateText = (date: string) =>
  new Date(date).toLocaleString(undefined, {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }) + " UTC";
async function api<T>(
  path: string,
  body?: unknown,
  method = body ? "POST" : "GET",
): Promise<T> {
  const response = await fetch("/api/admin/" + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired");
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.detail || data.error?.message || "Request failed");
  return data;
}
function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    el?.showModal();
    return () => el?.close();
  }, []);
  return (
    <dialog ref={ref} onCancel={close}>
      <div className="modal-head">
        <h2>{title}</h2>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={close}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export default function Dashboard() {
  const [me, setMe] = useState<Me | null>(null);
  const [clients, setClients] = useState<Client[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [kills, setKills] = useState<Kill[]>([]);
  const [keys, setKeys] = useState<Key[]>([]);
  const [reconciliation, setReconciliation] = useState<Reconciliation | null>(
    null,
  );
  const [tab, setTab] = useState("Overview");
  const [client, setClient] = useState("");
  const [period, setPeriod] = useState(new Date().toISOString().slice(0, 7));
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [keyValue, setKeyValue] = useState("");
  const [review, setReview] = useState<{
    id: string;
    action_name: string;
    summary: string;
    action: unknown;
  } | null>(null);
  const [modal, setModal] = useState<
    "workflow" | "user" | "credential" | "mapping" | "client" | null
  >(null);
  const [notice, setNotice] = useState("");
  const [report, setReport] = useState<MonthlyReport | null>(null);
  const reportClient = client || clients[0]?.id;
  useEffect(() => {
    if (tab !== "Reports" || !reportClient) return;
    let current = true;
    setReport(null);
    api<MonthlyReport>(`reports/${reportClient}/${period}`)
      .then((value) => {
        if (current) setReport(value);
      })
      .catch((error) => {
        if (current)
          setError(
            error instanceof Error ? error.message : "Report unavailable",
          );
      });
    return () => {
      current = false;
    };
  }, [tab, reportClient, period]);
  const load = useCallback(async () => {
    try {
      const [m, c, w, s, a, al, k, vk, r] = await Promise.all([
        api<Me>("auth/me"),
        api<Client[]>("clients"),
        api<Workflow[]>("workflows"),
        api<Spend>("spend?period=" + period),
        api<Approval[]>("approvals"),
        api<Alert[]>("alerts"),
        api<Kill[]>("kill"),
        api<Key[]>("keys"),
        api<Reconciliation>("reconciliation"),
      ]);
      setMe(m);
      setClients(c);
      setWorkflows(w);
      setSpend(s);
      setApprovals(a);
      setAlerts(al);
      setKills(k);
      setKeys(vk);
      setReconciliation(r);
      setError("");
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Unable to load dashboard",
      );
    } finally {
      setLoading(false);
    }
  }, [period]);
  useEffect(() => {
    void load();
  }, [load]);
  const operate = me?.role === "admin" || me?.role === "operator";
  const approve = me?.role === "admin" || me?.role === "approver";
  const shownWorkflows = workflows.filter(
    (w) => !client || w.client_id === client,
  );
  const shownSpend =
    spend?.workflows.filter((w) => !client || w.client_id === client) || [];
  const shownApprovals = approvals.filter((a) =>
    shownWorkflows.some((w) => w.id === a.workflow_id),
  );
  const shownAlerts = alerts.filter(
    (a) => (!client || a.client_id === client) && !a.details.resolved,
  );
  const pending = shownApprovals.filter((a) => a.status === "pending");
  const paused = (w: Workflow | SpendRow) =>
    kills.some(
      (k) =>
        k.active &&
        (k.scope === "global" ||
          (k.scope === "client" && k.scope_id === w.client_id) ||
          (k.scope === "workflow" && k.scope_id === w.id)),
    );
  const ownPause = (id: string) =>
    kills.some((k) => k.active && k.scope === "workflow" && k.scope_id === id);
  async function mutate(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      await load();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }
  async function signOut() {
    await mutate(async () => {
      const response = await fetch("/api/auth/logout", { method: "POST" });
      if (!response.ok) throw new Error("Sign out failed");
      window.location.assign("/login");
    });
  }
  async function createKey(id: string) {
    await mutate(async () => {
      const result = await api<{ key: string }>("keys", { workflow_id: id });
      setKeyValue(result.key);
    });
  }
  async function toggle(w: SpendRow) {
    await mutate(() =>
      api("kill", {
        scope: "workflow",
        scope_id: w.id,
        active: !ownPause(w.id),
        reason: "Dashboard control by " + me?.email,
      }),
    );
  }
  async function decision(choice: "approve" | "deny") {
    if (review)
      await mutate(async () => {
        await api("actions/" + review.id + "/decision", {
          decision: choice,
          approver: me?.email,
        });
        setReview(null);
      });
  }
  async function submitSetup(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const fields = Object.fromEntries(form.entries());
    await mutate(async () => {
      if (modal === "client") {
        await api("clients", {
          name: fields.name,
          retention_days: Number(fields.retention_days),
        });
      } else if (modal === "workflow") {
        const created = await api<{ id: string }>("workflows", {
          client_id: fields.client_id,
          name: fields.name,
          risk_class: fields.risk_class,
          policy: {
            models: String(fields.models)
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
            per_run_micro: Math.round(Number(fields.run_budget) * 1_000_000),
          },
        });
        await api("budgets", {
          workflow_id: created.id,
          period,
          cap_micro: Math.round(Number(fields.month_budget) * 1_000_000),
        });
      } else if (modal === "user")
        await api("users", { ...fields, client_id: fields.client_id || null });
      else if (modal === "credential")
        await api("provider-credentials", fields);
      else if (modal === "mapping")
        await api("provider-accounts", {
          ...fields,
          inference_key_id: fields.inference_key_id || null,
        });
      setModal(null);
      setNotice("Saved successfully.");
    });
  }
  const nav = [
    { name: "Overview", icon: LayoutDashboard },
    { name: "Workflows", icon: WorkflowIcon },
    { name: "Approvals", icon: ClipboardCheck },
    { name: "Alerts", icon: TriangleAlert },
    { name: "Reconciliation", icon: Activity },
    { name: "Reports", icon: FileDown },
    { name: "Settings", icon: SlidersHorizontal },
  ];
  const workflowTable = (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Workflow / client</th>
            <th>Status</th>
            <th>Monthly spend</th>
            <th>Budget</th>
            <th className="right">Controls</th>
          </tr>
        </thead>
        <tbody>
          {shownSpend.map((w) => (
            <tr key={w.id} data-testid={"workflow-" + w.id}>
              <td>
                <strong>{w.name}</strong>
                <span className="cell-sub">
                  {w.client} <span className="risk">{w.risk_class}</span>
                </span>
              </td>
              <td>
                <span className={"badge " + (paused(w) ? "amber" : "green")}>
                  <span className="dot" />
                  {paused(w) ? "Paused" : "Active"}
                </span>
              </td>
              <td className="numeric">
                {money(w.spent_micro)}
                {w.estimated_calls > 0 && <span className="muted"> · {money(w.estimated_micro)} estimated</span>}
                {w.recovery_pending > 0 && <span className="muted"> · {w.recovery_pending} recovery pending</span>}
                {w.reserved_micro > 0 && (
                  <span className="cell-sub">
                    {money(w.reserved_micro)} reserved
                  </span>
                )}
              </td>
              <td>
                {w.cap_micro ? (
                  <>
                    <span className="numeric">{money(w.cap_micro)}</span>
                    <div className="meter">
                      <i
                        style={{
                          width:
                            Math.min(
                              100,
                              (100 * (w.spent_micro + w.reserved_micro)) /
                                w.cap_micro,
                            ) + "%",
                        }}
                      />
                    </div>
                  </>
                ) : (
                  <span className="muted">No budget</span>
                )}
              </td>
              <td className="right">
                {operate ? (
                  <div className="controls">
                    <button
                      className="small"
                      onClick={() => createKey(w.id)}
                      disabled={busy}
                    >
                      <KeyRound size={14} />
                      Create key
                    </button>
                    <button
                      className={"small " + (ownPause(w.id) ? "" : "danger")}
                      onClick={() => toggle(w)}
                      disabled={busy}
                    >
                      {ownPause(w.id) ? (
                        <Play size={14} />
                      ) : (
                        <Pause size={14} />
                      )}{" "}
                      {ownPause(w.id) ? "Resume" : "Pause"}
                    </button>
                  </div>
                ) : (
                  <span className="muted">View only</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!shownSpend.length && (
        <div className="empty">
          <WorkflowIcon />
          <h3>No workflows yet</h3>
          <p>Add a client and workflow to start governing calls.</p>
        </div>
      )}
    </div>
  );
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/">
          <ShieldCheck size={27} />
          Governed
          <span className="brand-dot" />
        </a>
        <div className="workspace-name">
          <span className="workspace-icon">G</span>
          <div>
            Automation control<span>Starter workspace</span>
          </div>
          <ChevronDown size={16} />
        </div>
        <span className="nav-label">WORKSPACE</span>
        <nav>
          {nav.map(({ name, icon: Icon }) => (
            <button
              key={name}
              aria-current={tab === name ? "page" : undefined}
              className={tab === name ? "selected" : ""}
              onClick={() => setTab(name)}
            >
              <Icon size={18} />
              {name}
              {name === "Approvals" && pending.length > 0 && (
                <span className="nav-count">{pending.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <ShieldCheck size={20} />
          <strong>Governance is on</strong>
          <p>Calls are checked before they reach a provider.</p>
        </div>
        <div className="account">
          <span className="avatar">
            {me?.email.charAt(0).toUpperCase() || "·"}
          </span>
          <div>
            <strong title={me?.email}>{me?.email || "Loading…"}</strong>
            <span>
              {me?.role || ""}
              {me?.client_id ? " · client scoped" : ""}
            </span>
          </div>
          <button
            className="icon-button"
            aria-label="Sign out"
            onClick={() =>
              mutate(async () => {
                const response = await fetch("/api/auth/logout", {
                  method: "POST",
                });
                if (!response.ok) throw new Error("Sign out failed");
                window.location.assign("/login");
              })
            }
          >
            <LogOut size={16} />
          </button>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <span>
            Workspace <span className="slash">/</span> <strong>{tab}</strong>
          </span>
          <div>
            <span className="utc">UTC reporting</span>
            <button
              className="icon-button mobile-only"
              aria-label="Sign out"
              onClick={signOut}
            >
              <LogOut size={17} />
            </button>
            <button
              className="icon-button"
              aria-label="Refresh dashboard"
              onClick={() => load()}
            >
              <RefreshCw size={17} />
            </button>
          </div>
        </header>
        <div className="content">
          <section className="page-heading">
            <div>
              <span className="eyebrow">
                {tab === "Overview"
                  ? "YOUR AUTOMATION AT A GLANCE"
                  : "WORKSPACE / " + tab.toUpperCase()}
              </span>
              <h1>{tab === "Overview" ? "Everything, in view." : tab}</h1>
              <p className="muted">
                {tab === "Overview"
                  ? "A clear picture of spend, decisions, and workflow health."
                  : tab === "Workflows"
                    ? "Manage access and put a stop to any workflow."
                    : tab === "Approvals"
                      ? "Review the action before it takes place."
                      : tab === "Alerts"
                        ? "Investigate limits and unexplained provider differences."
                        : tab === "Reconciliation"
                          ? "Compare the gateway with your dedicated provider accounts."
                          : "Manage accounts, credentials, and provider mapping."}
              </p>
            </div>
            <div className="filters">
              <label className="sr-only" htmlFor="client-filter">
                Client
              </label>
              <select
                id="client-filter"
                value={client}
                onChange={(e) => setClient(e.target.value)}
              >
                <option value="">All accessible clients</option>
                {clients.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <label className="sr-only" htmlFor="month">
                Reporting month
              </label>
              <input
                id="month"
                type="month"
                value={period}
                onChange={(e) => setPeriod(e.target.value)}
              />
            </div>
          </section>
          {error && (
            <div className="error" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              <Check size={16} />
              {notice}
            </div>
          )}
          {loading ? (
            <div className="empty">Loading your workspace…</div>
          ) : (
            <>
              {tab === "Overview" && (
                <>
                  <section className="metrics">
                    <article>
                      <span>
                        MONTHLY SPEND <ArrowUpRight size={17} />
                      </span>
                      <strong data-testid="monthly-spend">
                        {money(
                          shownSpend.reduce((n, w) => n + w.spent_micro, 0),
                        )}
                      </strong>
                      <p>Recorded charges · {period}</p>
                    </article>
                    <article>
                      <span>
                        ACTIVE WORKFLOWS <WorkflowIcon size={17} />
                      </span>
                      <strong>
                        {shownWorkflows.filter((w) => !paused(w)).length}
                        <small> / {shownWorkflows.length}</small>
                      </strong>
                      <p>
                        {shownWorkflows.filter(paused).length} paused by
                        controls
                      </p>
                    </article>
                    <article>
                      <span>
                        AWAITING APPROVAL <ClipboardCheck size={17} />
                      </span>
                      <strong>{pending.length}</strong>
                      <p>Actions waiting for a human decision</p>
                    </article>
                    <article>
                      <span>
                        OPEN ALERTS <TriangleAlert size={17} />
                      </span>
                      <strong>{shownAlerts.length}</strong>
                      <p>Budget, delivery, and provider drift</p>
                    </article>
                  </section>
                  <section className="overview-grid">
                    <article className="panel">
                      <div className="panel-head">
                        <div>
                          <h2>Spend by client</h2>
                          <p>Actual charges this month</p>
                        </div>
                        <span className="pill">USD</span>
                      </div>
                      <div className="client-spend">
                        {(spend?.clients || [])
                          .filter((c) => !client || c.id === client)
                          .map((c, i) => (
                            <div key={c.id}>
                              <span className="client-icon">
                                {c.name.charAt(0)}
                              </span>
                              <div className="client-bar">
                                <strong>{c.name}</strong>
                                <div className="bar">
                                  <i
                                    style={{
                                      width:
                                        Math.max(
                                          2,
                                          (100 * c.spent_micro) /
                                            Math.max(
                                              1,
                                              ...(spend?.clients.map(
                                                (c) => c.spent_micro,
                                              ) || []),
                                            ),
                                        ) + "%",
                                      opacity: 1 - i * 0.08,
                                    }}
                                  />
                                </div>
                              </div>
                              <span className="numeric">
                                {money(c.spent_micro)}
                              </span>
                            </div>
                          ))}
                        {!spend?.clients.length && (
                          <p className="empty">
                            Client spend will appear after your first call.
                          </p>
                        )}
                      </div>
                    </article>
                    <article className="panel">
                      <div className="panel-head">
                        <div>
                          <h2>Needs your attention</h2>
                          <p>Decisions and controls</p>
                        </div>
                        <span className="attention-icon">
                          <Activity size={18} />
                        </span>
                      </div>
                      <div className="attention">
                        <button onClick={() => setTab("Approvals")}>
                          <span className="attention-square">
                            <ClipboardCheck size={20} />
                          </span>
                          <div>
                            <strong>
                              {pending.length
                                ? pending.length + " actions awaiting review"
                                : "No pending decisions"}
                            </strong>
                            <p>
                              {pending.length
                                ? "Review the proposed action and its content"
                                : "Approval requests will appear here"}
                            </p>
                          </div>
                          <ArrowUpRight size={18} />
                        </button>
                        <button onClick={() => setTab("Alerts")}>
                          <span className="attention-square amber">
                            <TriangleAlert size={20} />
                          </span>
                          <div>
                            <strong>
                              {shownAlerts.length
                                ? shownAlerts.length + " alerts to investigate"
                                : "No open alerts"}
                            </strong>
                            <p>Limits and provider differences</p>
                          </div>
                          <ArrowUpRight size={18} />
                        </button>
                      </div>
                      <div className="panel-foot">
                        <CircleHelp size={14} /> Gemini totals are
                        gateway-observed in v1.
                      </div>
                    </article>
                  </section>
                  <section className="panel">
                    <div className="panel-head">
                      <div>
                        <h2>Workflow activity</h2>
                        <p>Spend, budget headroom, and access controls</p>
                      </div>
                      <button
                        className="text-button"
                        onClick={() => setTab("Workflows")}
                      >
                        All workflows <ArrowUpRight size={16} />
                      </button>
                    </div>
                    {workflowTable}
                  </section>
                </>
              )}
              {tab === "Workflows" && (
                <>
                  <section className="panel">
                    <div className="panel-head">
                      <div>
                        <h2>Governed workflows</h2>
                        <p>
                          Pause takes effect across replicas within five
                          seconds.
                        </p>
                      </div>
                      {operate && (
                        <button
                          className="primary small"
                          onClick={() => setModal("workflow")}
                        >
                          <Plus size={16} />
                          New workflow
                        </button>
                      )}
                    </div>
                    {workflowTable}
                  </section>
                  <section className="panel space-top">
                    <div className="panel-head">
                      <div>
                        <h2>Virtual keys</h2>
                        <p>The full key is shown only when created.</p>
                      </div>
                      <KeyRound size={18} />
                    </div>
                    <div className="key-list">
                      {keys
                        .filter((k) =>
                          shownWorkflows.some((w) => w.id === k.workflow_id),
                        )
                        .map((k) => (
                          <div key={k.id}>
                            <code>{k.prefix}…</code>
                            <span>
                              {
                                shownWorkflows.find(
                                  (w) => w.id === k.workflow_id,
                                )?.name
                              }
                            </span>
                            <span className="muted">
                              {k.revoked_at ? "Revoked" : "Active"}
                            </span>
                            {operate && !k.revoked_at && (
                              <button
                                className="small danger"
                                onClick={() =>
                                  mutate(() =>
                                    api("keys/" + k.id, undefined, "DELETE"),
                                  )
                                }
                              >
                                Revoke
                              </button>
                            )}
                          </div>
                        ))}
                    </div>
                  </section>
                </>
              )}
              {tab === "Approvals" && (
                <section className="panel">
                  <div className="panel-head">
                    <div>
                      <h2>Action requests</h2>
                      <p>
                        Human approval and gateway token consumption are
                        required before execution.
                      </p>
                    </div>
                    <span className="pill">{pending.length} pending</span>
                  </div>
                  <div className="approval-list">
                    {shownApprovals.map((a) => (
                      <article key={a.id}>
                        <span className="attention-square">
                          <ClipboardCheck size={20} />
                        </span>
                        <div>
                          <h3>{a.action_name}</h3>
                          <p>{a.summary}</p>
                          <span className="muted">
                            {
                              shownWorkflows.find((w) => w.id === a.workflow_id)
                                ?.name
                            }{" "}
                            · expires {dateText(a.expires_at)}
                          </span>
                          {a.approver && (
                            <span className="cell-sub">
                              Decided by {a.approver}
                              {a.consumed ? " · consumed" : ""}
                            </span>
                          )}
                        </div>
                        <span
                          className={
                            "badge " + (a.status === "pending" ? "amber" : "")
                          }
                        >
                          {a.status}
                        </span>
                        {approve && (
                          <button
                            className="small"
                            onClick={() =>
                              mutate(async () =>
                                setReview(await api("actions/" + a.id)),
                              )
                            }
                          >
                            Review action
                          </button>
                        )}
                      </article>
                    ))}
                    {!shownApprovals.length && (
                      <div className="empty">
                        <ClipboardCheck />
                        <h3>No action requests</h3>
                        <p>
                          Your workflow&apos;s approval requests will appear
                          here.
                        </p>
                      </div>
                    )}
                  </div>
                </section>
              )}
              {tab === "Alerts" && (
                <section className="panel">
                  <div className="panel-head">
                    <div>
                      <h2>Open alerts</h2>
                      <p>
                        Possible bypass is an investigation flag; reporting
                        delays may explain it.
                      </p>
                    </div>
                    <TriangleAlert size={20} />
                  </div>
                  <div className="alert-list">
                    {shownAlerts.map((a) => (
                      <article key={a.id}>
                        <TriangleAlert size={20} />
                        <div>
                          <h3>{a.kind.replaceAll("_", " ")}</h3>
                          <p>
                            {clients.find((c) => c.id === a.client_id)?.name} ·{" "}
                            {dateText(a.created_at)}
                          </p>
                          <details>
                            <summary>Comparison details</summary>
                            <pre>{JSON.stringify(a.details, null, 2)}</pre>
                          </details>
                        </div>
                      </article>
                    ))}
                    {!shownAlerts.length && (
                      <div className="empty">
                        <ShieldCheck />
                        <h3>No open alerts</h3>
                        <p>
                          New governance and reconciliation alerts appear here.
                        </p>
                      </div>
                    )}
                  </div>
                </section>
              )}
              {tab === "Reconciliation" && (
                <>
                  <div className="info">
                    <Activity size={18} />
                    OpenAI and Anthropic use dedicated accounts. Gemini is
                    gateway-observed. Failed jobs mean provider totals are
                    unknown.
                  </div>
                  <section className="panel space-top">
                    <div className="panel-head">
                      <h2>Daily reporting jobs</h2>
                      <span className="pill">UTC</span>
                    </div>
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Client / provider</th>
                            <th>Day</th>
                            <th>Status</th>
                            <th>Details</th>
                          </tr>
                        </thead>
                        <tbody>
                          {reconciliation?.jobs
                            .filter((j) => !client || j.client_id === client)
                            .map((j) => (
                              <tr key={j.client_id + j.provider + j.day}>
                                <td>
                                  <strong>
                                    {
                                      clients.find((c) => c.id === j.client_id)
                                        ?.name
                                    }
                                  </strong>
                                  <span className="cell-sub">{j.provider}</span>
                                </td>
                                <td>{j.day}</td>
                                <td>
                                  <span
                                    className={
                                      "badge " +
                                      (j.status === "complete"
                                        ? "green"
                                        : "amber")
                                    }
                                  >
                                    {j.status}
                                  </span>
                                </td>
                                <td>
                                  {j.error || "Provider totals refreshed"}
                                </td>
                              </tr>
                            ))}
                        </tbody>
                      </table>
                      {!reconciliation?.jobs.length && (
                        <div className="empty">
                          <Activity />
                          <h3>No reporting jobs yet</h3>
                          <p>
                            Connect provider reporting credentials to begin
                            daily comparisons.
                          </p>
                        </div>
                      )}
                    </div>
                  </section>
                  <section className="panel space-top">
                    <div className="panel-head">
                      <h2>Gateway vs. provider</h2>
                    </div>
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Provider / dimension</th>
                            <th>Day</th>
                            <th>Gateway</th>
                            <th>Provider</th>
                          </tr>
                        </thead>
                        <tbody>
                          {reconciliation?.snapshots
                            .filter((s) => !client || s.client_id === client)
                            .map((s) => (
                              <tr
                                key={
                                  s.client_id + s.provider + s.day + s.dimension
                                }
                              >
                                <td>
                                  <strong>{s.dimension}</strong>
                                  <span className="cell-sub">{s.provider}</span>
                                </td>
                                <td>{s.day}</td>
                                <td>
                                  {s.kind === "cost"
                                    ? money(s.gateway.cost_micro)
                                    : `${s.gateway.input_tokens} in / ${s.gateway.output_tokens} out`}
                                </td>
                                <td>
                                  {s.kind === "cost"
                                    ? money(s.observed.cost_micro)
                                    : `${s.observed.input_tokens} in / ${s.observed.output_tokens} out`}
                                </td>
                              </tr>
                            ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                </>
              )}
              {tab === "Reports" && (
                <>
                  {!reportClient ? (
                    <section className="panel empty">
                      <FileDown />
                      <h3>No clients yet</h3>
                      <p>Add a client to create monthly reports.</p>
                    </section>
                  ) : !report ? (
                    <div className="empty">Loading report…</div>
                  ) : (
                    <>
                      <section className="panel">
                        <div className="panel-head">
                          <div>
                            <h2>
                              {report.client.name} · {report.period}
                            </h2>
                            <p>Monthly costs and savings, ready to share.</p>
                          </div>
                          <div className="controls">
                            <a
                              className="small export-link"
                              href={`/api/admin/reports/${reportClient}/${period}?format=csv`}
                            >
                              <FileDown size={15} />
                              Export CSV
                            </a>
                            <a
                              className="small export-link"
                              href={`/api/admin/reports/${reportClient}/${period}?format=html`}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              Printable HTML
                              <ArrowUpRight size={15} />
                            </a>
                          </div>
                        </div>
                        <div className="report-stats">
                          <div>
                            <span>Total gateway cost</span>
                            <strong>{money(report.totals.cost_micro)}</strong>
                          </div>
                          <div>
                            <span>Provider calls</span>
                            <strong>{report.totals.calls}</strong>
                          </div>
                          <div>
                            <span>Hours saved</span>
                            <strong>
                              {report.hours_saved
                                ? Number(
                                    report.hours_saved.hours,
                                  ).toLocaleString(undefined, {
                                    maximumFractionDigits: 2,
                                  })
                                : "Not supplied"}
                            </strong>
                            <span>{report.hours_saved?.basis}</span>
                          </div>
                        </div>
                        <div className="table-wrap">
                          <table>
                            <thead>
                              <tr>
                                <th>Workflow</th>
                                <th>Calls</th>
                                <th>Estimated calls</th>
                                <th>Cost (USD)</th>
                              </tr>
                            </thead>
                            <tbody>
                              {report.workflows.map((w) => (
                                <tr key={w.id || "unassigned"}>
                                  <td>{w.name}</td>
                                  <td>{w.calls}</td>
                                  <td>{w.estimated_calls}</td>
                                  <td className="numeric">
                                    {money(w.cost_micro)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                        <div className="panel-foot">
                          Totals follow the original UTC accounting period, including later corrections.{" "}
                          {report.totals.estimated_calls} calls ({money(report.totals.estimated_cost_micro)}) remain estimated.{" "}
                          {report.totals.corrected_calls} calls were corrected; {report.totals.recovery_pending} recovery entries await processing.
                        </div>
                      </section>
                      <section className="panel space-top">
                        <div className="panel-head">
                          <div>
                            <h2>Savings evidence</h2>
                            <p>
                              Supply saved time in minutes; exports convert it
                              to hours.
                            </p>
                          </div>
                          <ClipboardCheck size={18} />
                        </div>
                        <div className="settings-body">
                          {operate ? (
                            <form
                              key={reportClient + period}
                              onSubmit={(event) => {
                                event.preventDefault();
                                const fields = new FormData(
                                  event.currentTarget,
                                );
                                void mutate(async () => {
                                  await api(
                                    `reports/${reportClient}/${period}/hours`,
                                    {
                                      minutes_saved: Number(
                                        fields.get("minutes_saved"),
                                      ),
                                      basis: fields.get("basis"),
                                      note: fields.get("note"),
                                    },
                                  );
                                  setReport(
                                    await api<MonthlyReport>(
                                      `reports/${reportClient}/${period}`,
                                    ),
                                  );
                                  setNotice("Savings evidence saved.");
                                });
                              }}
                            >
                              <div className="form-row">
                                <label>
                                  Minutes saved
                                  <input
                                    name="minutes_saved"
                                    type="number"
                                    min="0"
                                    step="1"
                                    required
                                    defaultValue={
                                      report.hours_saved?.minutes || 0
                                    }
                                  />
                                </label>
                                <label>
                                  Evidence basis
                                  <select
                                    name="basis"
                                    defaultValue={
                                      report.hours_saved?.basis || "estimate"
                                    }
                                  >
                                    <option value="estimate">Estimate</option>
                                    <option value="measured">Measured</option>
                                  </select>
                                </label>
                              </div>
                              <label>
                                Evidence note
                                <textarea
                                  name="note"
                                  maxLength={2000}
                                  defaultValue={report.hours_saved?.note || ""}
                                  placeholder="Baseline work time, time measured, and how this figure was calculated"
                                />
                              </label>
                              <button className="primary small" disabled={busy}>
                                <Check size={16} />
                                Save savings
                              </button>
                            </form>
                          ) : (
                            <p>
                              {report.hours_saved?.note ||
                                "No savings evidence supplied."}
                            </p>
                          )}
                        </div>
                      </section>
                    </>
                  )}
                </>
              )}
              {tab === "Settings" && (
                <section className="settings-grid">
                  <article className="panel">
                    <div className="panel-head">
                      <div>
                        <h2>Provider access</h2>
                        {me?.role === "admin" && (
                          <button
                            className="small"
                            onClick={() => setModal("client")}
                          >
                            <Plus size={15} />
                            New client
                          </button>
                        )}
                        <p>Credentials are encrypted at rest.</p>
                      </div>
                      <KeyRound size={19} />
                    </div>
                    <div className="settings-body">
                      <p>
                        Each client needs dedicated inference credentials and a
                        provider account for reconciliation.
                      </p>
                      {operate && (
                        <>
                          <button
                            className="small"
                            onClick={() => setModal("credential")}
                          >
                            Add provider credential
                          </button>
                          <button
                            className="small"
                            onClick={() => setModal("mapping")}
                          >
                            Map provider account
                          </button>
                        </>
                      )}
                      <ul>
                        {reconciliation?.accounts.map((a) => (
                          <li key={a.client_id + a.provider}>
                            {clients.find((c) => c.id === a.client_id)?.name} ·{" "}
                            {a.provider}
                            <span className="cell-sub">
                              {a.external_id} ·{" "}
                              {a.validated ? "Validated" : "Mock mapping"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </article>
                  <article className="panel">
                    <div className="panel-head">
                      <div>
                        <h2>People & permissions</h2>
                        <p>Access follows role and client scope.</p>
                      </div>
                      <ShieldCheck size={19} />
                    </div>
                    <div className="settings-body">
                      <p>
                        Administrators manage accounts. Operators manage
                        workflows. Approvers decide actions. Viewers can read
                        their workspace.
                      </p>
                      {me?.role === "admin" && (
                        <button
                          className="small"
                          onClick={() => setModal("user")}
                        >
                          <Plus size={15} />
                          Add account
                        </button>
                      )}
                      <p className="muted">Your current role: {me?.role}</p>
                    </div>
                  </article>
                </section>
              )}
            </>
          )}
          <footer className="page-footer">
            <span>
              <ShieldCheck size={14} /> Governed Automation Starter
            </span>
            <span>Native providers · UTC · USD</span>
          </footer>
        </div>
      </main>
      {keyValue && (
        <Modal title="Your new virtual key" close={() => setKeyValue("")}>
          <p>Copy this key now. It will not be shown again.</p>
          <code className="new-key" data-testid="new-key">
            {keyValue}
          </code>
          <button
            className="primary"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(keyValue);
                setNotice("Key copied.");
              } catch {
                setError("Select and copy the key manually.");
              }
            }}
          >
            <Copy size={17} />
            Copy key
          </button>
        </Modal>
      )}
      {review && (
        <Modal title={review.action_name} close={() => setReview(null)}>
          <p>{review.summary}</p>
          <pre>{JSON.stringify(review.action, null, 2)}</pre>
          {approvals.find((a) => a.id === review.id)?.status === "pending" && (
            <div className="controls">
              <button
                className="primary"
                disabled={busy}
                onClick={() => decision("approve")}
              >
                <Check size={17} />
                Approve
              </button>
              <button
                className="small danger"
                disabled={busy}
                onClick={() => decision("deny")}
              >
                <X size={17} />
                Deny
              </button>
            </div>
          )}
        </Modal>
      )}
      {modal && (
        <Modal
          title={
            modal === "client"
              ? "New client"
              : modal === "workflow"
                ? "New governed workflow"
                : modal === "user"
                  ? "Add account"
                  : modal === "credential"
                    ? "Add provider credential"
                    : "Map provider account"
          }
          close={() => setModal(null)}
        >
          <form onSubmit={submitSetup}>
            {modal === "user" ? (
              <>
                <label>
                  Email
                  <input name="email" type="email" required />
                </label>
                <label>
                  Password
                  <input
                    name="password"
                    type="password"
                    minLength={12}
                    required
                  />
                </label>
                <label>
                  Role
                  <select name="role">
                    <option>viewer</option>
                    <option>approver</option>
                    <option>operator</option>
                    <option>admin</option>
                  </select>
                </label>
              </>
            ) : null}
            {modal !== "client" && (
              <label>
                Client
                <select name="client_id" required={modal !== "user"}>
                  {modal === "user" && <option value="">All clients</option>}
                  {clients.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {modal === "client" && (
              <>
                <label>
                  Client name
                  <input name="name" required />
                </label>
                <label>
                  Retention days
                  <input
                    name="retention_days"
                    type="number"
                    min="1"
                    defaultValue="180"
                    required
                  />
                </label>
              </>
            )}
            {modal === "workflow" && (
              <>
                <label>
                  Workflow name
                  <input name="name" required />
                </label>
                <label>
                  Risk class
                  <select name="risk_class">
                    <option>R1</option>
                    <option>R0</option>
                    <option>R2</option>
                    <option>R3</option>
                  </select>
                </label>
                <label>
                  Allowed models, comma separated
                  <input name="models" placeholder="gpt-4.1-mini" required />
                </label>
                <div className="form-row">
                  <label>
                    Monthly budget (USD)
                    <input
                      name="month_budget"
                      type="number"
                      min="0.01"
                      step="0.01"
                      defaultValue="40"
                      required
                    />
                  </label>
                  <label>
                    Per-run budget (USD)
                    <input
                      name="run_budget"
                      type="number"
                      min="0.01"
                      step="0.01"
                      defaultValue="0.50"
                      required
                    />
                  </label>
                </div>
              </>
            )}
            {(modal === "credential" || modal === "mapping") && (
              <>
                <label>
                  Provider
                  <select name="provider">
                    <option>openai</option>
                    <option>anthropic</option>
                    {modal === "credential" && <option>gemini</option>}
                  </select>
                </label>
                <label>
                  Credential label
                  <input
                    name={modal === "credential" ? "label" : "credential_label"}
                    defaultValue="default"
                    required
                  />
                </label>
              </>
            )}
            {modal === "credential" && (
              <label>
                Provider API key
                <input name="key" type="password" autoComplete="off" required />
              </label>
            )}
            {modal === "mapping" && (
              <>
                <label>
                  Project or workspace ID
                  <input name="external_id" required />
                </label>
                <label>
                  Anthropic inference key ID
                  <input name="inference_key_id" />
                </label>
                <p className="muted">
                  Live validation requires the reporting key configured on the
                  admin service.
                </p>
              </>
            )}
            <button className="primary" disabled={busy}>
              {busy ? "Saving…" : "Save"}
              <Check size={16} />
            </button>
          </form>
        </Modal>
      )}
    </div>
  );
}
