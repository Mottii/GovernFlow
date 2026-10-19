"use client";
import { useEffect, useRef, useState } from "react";
import { controlApi, micro, type ControlWorkflow } from "./controlApi";
type Condition = {
  path: string;
  op: string;
  value: unknown;
  include_subdomains?: boolean;
};
type Rule = {
  id: string;
  action_name: string;
  outcome: string;
  conditions: Condition[];
  risk_class?: string;
  timeout_seconds?: number;
  on_timeout?: string;
};
type Policy = {
  models?: string[];
  rules?: Rule[];
  rpm?: number;
  per_run_micro?: number;
  max_output_tokens?: number;
  loop_breaker?: { max_steps?: number; rpm?: number; run_cost_micro?: number };
  stop_escalation?: string;
  outage?: { mode: string };
  approval_channels?: string[];
  [key: string]: unknown;
};
type Revision = {
  version: number;
  risk_class: string;
  policy: Policy;
  author: string;
  hash: string;
  source_version: number | null;
};
type History = {
  active_version: number;
  review_required: boolean;
  revisions: Revision[];
};
function LiteralInput({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const [text, setText] = useState(JSON.stringify(value));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setText(JSON.stringify(value));
  }, [value]);
  return (
    <label>
      Comparison value (JSON)
      <input
        value={text}
        aria-invalid={invalid}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setInvalid(false);
          } catch {
            setInvalid(true);
          }
        }}
        onBlur={(e) => {
          if (invalid) e.target.setCustomValidity("Enter a valid JSON value");
          else e.target.setCustomValidity("");
        }}
      />
      {invalid && (
        <span role="alert">Use a JSON string, number, boolean, or list.</span>
      )}
    </label>
  );
}
export default function PolicyBuilder({
  workflows,
  role,
}: {
  workflows: ControlWorkflow[];
  role: string;
}) {
  const builderRef = useRef<HTMLElement>(null);
  const [workflow, setWorkflow] = useState(""),
    [history, setHistory] = useState<History>({
      active_version: 0,
      review_required: false,
      revisions: [],
    });
  const [policy, setPolicy] = useState<Policy>({ models: [], rules: [] }),
    [risk, setRisk] = useState("R1"),
    [reason, setReason] = useState("");
  const [actionName, setActionName] = useState("send"),
    [sample, setSample] = useState(
      '{"recipient":"person@example.test","amount":10}',
    );
  const [preview, setPreview] = useState<unknown>(null),
    [compare, setCompare] = useState(0),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const writable = role === "admin" || role === "operator";
  async function load(identity: string, reset = false) {
    const result = await controlApi<History>(
      `workflows/${identity}/policy-revisions`,
    );
    setHistory(result);
    if (reset) {
      const current =
        result.revisions.find((r) => r.version === result.active_version) ||
        result.revisions[0];
      setPolicy(current?.policy || { models: [], rules: [] });
      setRisk(current?.risk_class || "R1");
      setCompare(0);
      setPreview(null);
    }
  }
  useEffect(() => {
    if (workflow) load(workflow, true).catch((e) => setError(e.message));
  }, [workflow]);
  async function act(operation: () => Promise<void>, needsReason = true) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      if (builderRef.current?.querySelector('[aria-invalid="true"]'))
        throw new Error("Correct invalid comparison JSON before continuing");
      if (needsReason && !reason.trim())
        throw new Error("Add a reason for this change");
      await operation();
      await load(workflow);
      setNotice("Operation completed.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Operation failed");
    } finally {
      setBusy(false);
    }
  }
  function setting(name: string, value: unknown) {
    setPolicy((p) => ({ ...p, [name]: value }));
    setPreview(null);
  }
  function rule(index: number, change: Partial<Rule>) {
    setting(
      "rules",
      (policy.rules || []).map((r, i) =>
        i === index ? { ...r, ...change } : r,
      ),
    );
  }
  const selected = history.revisions.find((r) => r.version === compare);
  return (
    <section
      ref={builderRef}
      className="panel control-panel"
      aria-label="Policy builder"
    >
      <h2>Policy builder</h2>
      <p>
        All conditions within a rule must match. Deny takes priority, then human
        review, then allow. Unmatched actions are denied.
      </p>
      <label>
        Policy workflow
        <select value={workflow} onChange={(e) => setWorkflow(e.target.value)}>
          <option value="">Choose…</option>
          {workflows.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      </label>
      {error && (
        <p role="alert" className="control-error">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {workflow && (
        <>
          <p>
            Active revision:{" "}
            {history.active_version || "Awaiting administrator publication"}
            {history.review_required && " · Administrative review required"}
          </p>
          <fieldset disabled={!writable || busy}>
            <legend>Workflow settings</legend>
            <div className="control-grid">
              <label>
                Workflow risk
                <select
                  value={risk}
                  onChange={(e) => {
                    setRisk(e.target.value);
                    if (e.target.value >= "R2")
                      setting("outage", { mode: "closed" });
                    setPreview(null);
                  }}
                >
                  {["R0", "R1", "R2", "R3"].map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
              </label>
              <label>
                Allowed model patterns
                <input
                  value={(policy.models || []).join(", ")}
                  onChange={(e) =>
                    setting(
                      "models",
                      e.target.value
                        .split(",")
                        .map((v) => v.trim())
                        .filter(Boolean),
                    )
                  }
                />
              </label>
              <label>
                Run cap (USD, blank for no cap)
                <input
                  type="number"
                  min="0.000001"
                  step="0.000001"
                  value={
                    policy.per_run_micro === undefined
                      ? ""
                      : policy.per_run_micro / 1_000_000
                  }
                  onChange={(e) => {
                    try {
                      if (!e.target.value) {
                        const next = { ...policy };
                        delete next.per_run_micro;
                        setPolicy(next);
                      } else setting("per_run_micro", micro(e.target.value));
                      setError("");
                    } catch (error) {
                      setError(
                        error instanceof Error
                          ? error.message
                          : "Invalid run cap",
                      );
                    }
                  }}
                />
              </label>
              <label>
                Requests per minute
                <input
                  type="number"
                  min="1"
                  max="1000000"
                  value={policy.rpm ?? 60}
                  onChange={(e) => setting("rpm", Number(e.target.value))}
                />
              </label>
              <label>
                Maximum output tokens
                <input
                  type="number"
                  min="1"
                  max="32768"
                  value={policy.max_output_tokens ?? 32768}
                  onChange={(e) =>
                    setting("max_output_tokens", Number(e.target.value))
                  }
                />
              </label>
              <label>
                Maximum run steps
                <input
                  type="number"
                  min="1"
                  value={policy.loop_breaker?.max_steps ?? 100}
                  onChange={(e) =>
                    setting("loop_breaker", {
                      ...policy.loop_breaker,
                      max_steps: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label>
                Loop request cap per minute
                <input
                  type="number"
                  min="1"
                  max="1000000"
                  value={policy.loop_breaker?.rpm ?? 120}
                  onChange={(e) =>
                    setting("loop_breaker", {
                      ...policy.loop_breaker,
                      rpm: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label>
                Run-limit escalation
                <select
                  value={policy.stop_escalation || "run"}
                  onChange={(e) => setting("stop_escalation", e.target.value)}
                >
                  <option value="run">Stop offending run</option>
                  <option value="workflow">Also stop workflow</option>
                  <option value="client">Also stop client</option>
                </select>
              </label>
              <label>
                Database outage behavior
                <select
                  value={policy.outage?.mode || "closed"}
                  onChange={(e) => setting("outage", { mode: e.target.value })}
                >
                  <option value="closed">Block calls</option>
                  <option value="open" disabled={risk >= "R2"}>
                    Permit eligible single-node cached calls
                  </option>
                </select>
              </label>
            </div>
            <p>
              Only administrator publication changes live settings. Cached
              outage calls also require deployment opt-in.
            </p>
            <fieldset>
              <legend>Approval notification channels</legend>
              <p>Selected channels use the client’s assigned destinations.</p>
              {["telegram", "email", "webhook"].map((channel) => (
                <label className="control-check" key={channel}>
                  <input
                    type="checkbox"
                    checked={(
                      policy.approval_channels ?? [
                        "telegram",
                        "email",
                        "webhook",
                      ]
                    ).includes(channel)}
                    onChange={(e) => {
                      const current = policy.approval_channels ?? [
                        "telegram",
                        "email",
                        "webhook",
                      ];
                      setting(
                        "approval_channels",
                        e.target.checked
                          ? [...current, channel]
                          : current.filter((c) => c !== channel),
                      );
                    }}
                  />
                  {channel === "telegram"
                    ? "Telegram"
                    : channel === "email"
                      ? "Email"
                      : "Webhook"}
                </label>
              ))}
            </fieldset>
          </fieldset>
          <fieldset disabled={!writable || busy}>
            <legend>Action rules</legend>
            {(policy.rules || []).map((r, i) => (
              <article className="control-rule" key={i}>
                <div className="control-grid">
                  <label>
                    Rule ID
                    <input
                      value={r.id}
                      onChange={(e) => rule(i, { id: e.target.value })}
                    />
                  </label>
                  <label>
                    Action name
                    <input
                      value={r.action_name}
                      onChange={(e) => rule(i, { action_name: e.target.value })}
                    />
                  </label>
                  <label>
                    Outcome
                    <select
                      value={r.outcome}
                      onChange={(e) => rule(i, { outcome: e.target.value })}
                    >
                      <option value="deny">Deny</option>
                      <option value="review">Human review</option>
                      <option value="allow">
                        Allow, subject to risk floor
                      </option>
                    </select>
                  </label>
                  <label>
                    Rule risk floor
                    <select
                      value={r.risk_class || "R0"}
                      onChange={(e) => rule(i, { risk_class: e.target.value })}
                    >
                      {["R0", "R1", "R2", "R3"].map((v) => (
                        <option key={v}>{v}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Review timeout (seconds)
                    <input
                      type="number"
                      min="1"
                      max="604800"
                      value={r.timeout_seconds ?? 3600}
                      onChange={(e) =>
                        rule(i, { timeout_seconds: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label>
                    Timeout outcome
                    <select
                      value={r.on_timeout || "deny"}
                      onChange={(e) => rule(i, { on_timeout: e.target.value })}
                    >
                      <option value="deny">Deny</option>
                      <option value="allow">Allow only at R0/R1</option>
                    </select>
                  </label>
                </div>
                {r.conditions.map((c, j) => (
                  <div className="control-condition" key={j}>
                    <label>
                      Field path
                      <input
                        placeholder="/recipient"
                        value={c.path}
                        onChange={(e) =>
                          rule(i, {
                            conditions: r.conditions.map((v, k) =>
                              k === j ? { ...v, path: e.target.value } : v,
                            ),
                          })
                        }
                      />
                    </label>
                    <label>
                      Condition
                      <select
                        value={c.op}
                        onChange={(e) =>
                          rule(i, {
                            conditions: r.conditions.map((v, k) =>
                              k === j
                                ? {
                                    ...v,
                                    op: e.target.value,
                                    value:
                                      e.target.value === "exists"
                                        ? true
                                        : e.target.value === "in" ||
                                            e.target.value ===
                                              "recipient_domain_in"
                                          ? ["example.test"]
                                          : e.target.value === "eq"
                                            ? "value"
                                            : 0,
                                    include_subdomains: false,
                                  }
                                : v,
                            ),
                          })
                        }
                      >
                        {[
                          "exists",
                          "eq",
                          "in",
                          "lt",
                          "lte",
                          "gt",
                          "gte",
                          "recipient_domain_in",
                        ].map((v) => (
                          <option key={v}>{v}</option>
                        ))}
                      </select>
                    </label>
                    <LiteralInput
                      value={c.value}
                      onChange={(value) =>
                        rule(i, {
                          conditions: r.conditions.map((v, k) =>
                            k === j ? { ...v, value } : v,
                          ),
                        })
                      }
                    />
                    {c.op === "recipient_domain_in" && (
                      <label className="control-check">
                        <input
                          type="checkbox"
                          checked={!!c.include_subdomains}
                          onChange={(e) =>
                            rule(i, {
                              conditions: r.conditions.map((v, k) =>
                                k === j
                                  ? {
                                      ...v,
                                      include_subdomains: e.target.checked,
                                    }
                                  : v,
                              ),
                            })
                          }
                        />
                        Include subdomains
                      </label>
                    )}
                    <button
                      onClick={() =>
                        rule(i, {
                          conditions: r.conditions.filter((_, k) => k !== j),
                        })
                      }
                    >
                      Remove condition
                    </button>
                  </div>
                ))}
                <button
                  onClick={() =>
                    rule(i, {
                      conditions: [
                        ...r.conditions,
                        { path: "/amount", op: "gte", value: 100 },
                      ],
                    })
                  }
                >
                  Add condition
                </button>{" "}
                <button
                  onClick={() =>
                    setting(
                      "rules",
                      (policy.rules || []).filter((_, k) => k !== i),
                    )
                  }
                >
                  Remove rule
                </button>
              </article>
            ))}
            <button
              onClick={() =>
                setting("rules", [
                  ...(policy.rules || []),
                  {
                    id: crypto.randomUUID(),
                    action_name: "send",
                    outcome: "review",
                    conditions: [],
                  },
                ])
              }
            >
              Add rule
            </button>
          </fieldset>
          {writable && (
            <>
              <label>
                Change reason
                <input
                  value={reason}
                  maxLength={200}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <button
                className="primary small"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    await controlApi(
                      `workflows/${workflow}/policy`,
                      {
                        expected_active_version: history.active_version,
                        risk_class: risk,
                        policy,
                        reason,
                      },
                      "PUT",
                    );
                  })
                }
              >
                Save policy draft
              </button>
              <h3>Try an action</h3>
              <label>
                Preview action name
                <input
                  value={actionName}
                  onChange={(e) => setActionName(e.target.value)}
                />
              </label>
              <label>
                Sample action JSON
                <textarea
                  value={sample}
                  onChange={(e) => setSample(e.target.value)}
                  rows={4}
                />
              </label>
              <button
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    setPreview(
                      await controlApi("policies/preview", {
                        workflow_id: workflow,
                        risk_class: risk,
                        policy,
                        action_name: actionName,
                        action: JSON.parse(sample),
                      }),
                    );
                  }, false)
                }
              >
                Preview action
              </button>
              {preview !== null && (
                <pre
                  className="control-code"
                  aria-label="Policy preview result"
                >
                  {JSON.stringify(preview, null, 2)}
                </pre>
              )}
            </>
          )}
          <h3>Revision history</h3>
          <p>
            Publishing or rollback invalidates outstanding reviews and
            unconsumed authorizations. Callers must submit a fresh check.
          </p>
          <label>
            Compare or load revision
            <select
              value={compare}
              onChange={(e) => setCompare(Number(e.target.value))}
            >
              <option value="0">Choose…</option>
              {history.revisions.map((r) => (
                <option key={r.version} value={r.version}>
                  Revision {r.version}
                  {r.version === history.active_version
                    ? " · active"
                    : ""} · {r.author}
                </option>
              ))}
            </select>
          </label>
          {selected && (
            <>
              <div className="control-grid">
                <div>
                  <h4>Revision {selected.version}</h4>
                  <pre className="control-code">
                    {JSON.stringify(selected.policy, null, 2)}
                  </pre>
                </div>
                <div>
                  <h4>Current editor</h4>
                  <pre className="control-code">
                    {JSON.stringify(policy, null, 2)}
                  </pre>
                </div>
              </div>
              {writable && (
                <button
                  onClick={() => {
                    setPolicy(selected.policy);
                    setRisk(selected.risk_class);
                    setPreview(null);
                  }}
                >
                  Load revision into editor
                </button>
              )}
              {role === "admin" && (
                <>
                  {selected.version > history.active_version && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await controlApi(
                            `workflows/${workflow}/policy-revisions/${selected.version}/activate`,
                            {
                              expected_active_version: history.active_version,
                              reason,
                            },
                          );
                        })
                      }
                    >
                      Publish revision {selected.version}
                    </button>
                  )}
                  {selected.version < history.active_version && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await controlApi(
                            `workflows/${workflow}/policy-revisions/${selected.version}/rollback`,
                            {
                              expected_active_version: history.active_version,
                              reason,
                            },
                          );
                        })
                      }
                    >
                      Publish rollback as new revision
                    </button>
                  )}
                </>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
