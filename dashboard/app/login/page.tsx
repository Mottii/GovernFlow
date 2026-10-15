"use client";
import { useState } from "react";
import { ShieldCheck, ArrowRight } from "lucide-react";
export default function Login() {
  const [error, setError] = useState("");
  const [agencies, setAgencies] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: form.get("email"),
          password: form.get("password"),
          ...(form.get("agency_id") ? {agency_id: form.get("agency_id")} : {}),
        }),
      });
      const data = await response.json();
      if (response.status === 409 && data.detail?.agency_ids) {
        setAgencies(data.detail.agency_ids);
        setBusy(false);
        return;
      }
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Sign in failed");
      window.location.assign("/");
    } catch (error) {
      setError(error instanceof Error ? error.message : "Sign in failed");
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <section className="login-story">
        <a className="brand" href="/">
          <ShieldCheck size={28} /> Governed
          <span className="brand-dot" />
        </a>
        <div>
          <span className="eyebrow">AUTOMATION, WITH ACCOUNTABILITY</span>
          <h1>
            Keep the work moving.
            <br />
            Keep control.
          </h1>
          <p>
            One place to see what your workflows spend, review what they do, and
            pause them when you need to.
          </p>
        </div>
        <footer>
          Governed Automation Starter <span>01 / CONTROL</span>
        </footer>
      </section>
      <section className="login-form">
        <div>
          <span className="eyebrow">YOUR CONTROL ROOM</span>
          <h2>Welcome back</h2>
          <p className="muted">
            Sign in with your administrator-provided account.
          </p>
          <form onSubmit={submit}>
            <label>
              Email
              <input
                name="email"
                type="email"
                autoComplete="username"
                required
              />
            </label>
            <label>
              Password
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            {agencies.length > 0 && <label>Agency
              <select name="agency_id" required>{agencies.map(id => <option key={id} value={id}>{id}</option>)}</select>
            </label>}
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            <button className="primary" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
              <ArrowRight size={18} />
            </button>
          </form>
          <p className="login-note">
            Your role determines which clients and controls you can access.
          </p>
        </div>
      </section>
    </main>
  );
}
