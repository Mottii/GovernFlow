"use client";
import { useCallback, useEffect, useState } from "react";
import { controlApi, type ControlClient } from "./controlApi";
type Version = {
  id: string;
  version: number;
  state: string;
  created_at: string;
  author: string;
};
type Slot = {
  id: string;
  client_id: string;
  provider: string;
  label: string;
  active_version: number;
  versions: Version[];
};
export default function CredentialControls({
  clients,
  writable,
}: {
  clients: ControlClient[];
  writable: boolean;
}) {
  const [slots, setSlots] = useState<Slot[]>([]),
    [client, setClient] = useState(""),
    [provider, setProvider] = useState("openai"),
    [label, setLabel] = useState("default"),
    [selected, setSelected] = useState(""),
    [secret, setSecret] = useState(""),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const load = useCallback(
    async () => setSlots(await controlApi<Slot[]>("provider-credentials")),
    [],
  );
  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, [load]);
  async function act(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (!reason.trim()) throw new Error("Add a reason for this change");
      await operation();
      setSecret("");
      await load();
      setNotice(
        "Credential metadata updated. No stored plaintext is returned.",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Change failed");
    } finally {
      setSecret("");
      setBusy(false);
    }
  }
  return (
    <section className="panel control-panel" aria-label="Credential controls">
      <h2>Provider credentials</h2>
      <p>
        Stage a replacement before activation. Admitted calls retain their
        selected version. Retiring a version does not revoke the external
        provider key.
      </p>
      {error && (
        <p role="alert" className="control-error">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {writable && (
        <form
          className="control-grid"
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              if (selected) {
                const slot = slots.find((s) => s.id === selected)!;
                await controlApi(`provider-credentials/${selected}/versions`, {
                  key: secret,
                  expected_version: Math.max(
                    0,
                    ...slot.versions.map((v) => v.version),
                  ),
                  reason,
                });
              } else
                await controlApi("provider-credentials", {
                  client_id: client,
                  provider,
                  label,
                  key: secret,
                });
            });
          }}
        >
          <label>
            Credential slot
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">Create a new slot</option>
              {slots.map((s) => (
                <option key={s.id} value={s.id}>
                  {clients.find((c) => c.id === s.client_id)?.name} ·{" "}
                  {s.provider} · {s.label}
                </option>
              ))}
            </select>
          </label>
          {!selected && (
            <>
              <label>
                Credential client
                <select
                  required
                  value={client}
                  onChange={(e) => setClient(e.target.value)}
                >
                  <option value="">Choose…</option>
                  {clients.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Provider
                <select
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                >
                  {["openai", "anthropic", "gemini"].map((p) => (
                    <option key={p}>{p}</option>
                  ))}
                </select>
              </label>
              <label>
                Slot label
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  required
                />
              </label>
            </>
          )}
          <label>
            New provider key
            <input
              type="password"
              autoComplete="new-password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              required
            />
          </label>
          <label>
            Credential change reason
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              required
              maxLength={200}
            />
          </label>
          <button className="primary small" disabled={busy}>
            {selected ? "Stage replacement" : "Create credential"}
          </button>
        </form>
      )}
      {slots.map((s) => (
        <article className="control-rule" key={s.id}>
          <h3>
            {clients.find((c) => c.id === s.client_id)?.name} · {s.provider} ·{" "}
            {s.label}
          </h3>
          <p>
            {s.active_version
              ? `Active version ${s.active_version}`
              : "No active version — new calls are blocked"}
          </p>
          <p className="muted">
            Activation clears account validation. Strict mapped routes stay
            blocked until their provider account is revalidated.
          </p>
          <ul>
            {s.versions.map((v) => (
              <li key={v.id}>
                Version {v.version} · {v.state} · {v.author}{" "}
                {writable && v.state === "staged" && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        await controlApi(
                          `provider-credentials/${s.id}/versions/${v.version}/activate`,
                          { expected_active_version: s.active_version, reason },
                        );
                      })
                    }
                  >
                    Activate version {v.version}
                  </button>
                )}{" "}
                {writable && v.state !== "retired" && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        await controlApi(
                          `provider-credentials/${s.id}/versions/${v.version}/retire`,
                          { expected_active_version: s.active_version, reason },
                        );
                      })
                    }
                  >
                    Retire version {v.version}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </article>
      ))}
    </section>
  );
}
