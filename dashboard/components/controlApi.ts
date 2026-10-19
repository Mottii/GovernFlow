export async function controlApi<T>(
  path: string,
  body?: unknown,
  method = body ? "POST" : "GET",
): Promise<T> {
  const response = await fetch("/api/admin/" + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired");
  }
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok)
    throw new Error(
      typeof data?.detail === "string"
        ? data.detail
        : JSON.stringify(
            data?.detail || data?.error?.message || "Request failed",
          ),
    );
  return data;
}
export type ControlClient = { id: string; name: string };
export type ControlWorkflow = {
  id: string;
  client_id: string;
  name: string;
  risk_class: string;
  policy: Record<string, unknown>;
};
export const usd = (n: number | null) =>
  n === null
    ? "No client cap"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 6,
      }).format(n / 1_000_000);
export function micro(value: string): number {
  if (!/^\d+(\.\d{1,6})?$/.test(value))
    throw new Error(
      "Enter a nonnegative USD amount with at most six decimal places",
    );
  const [whole, fraction = ""] = value.split(".");
  const result = BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"));
  if (result > BigInt(Number.MAX_SAFE_INTEGER))
    throw new Error("Amount exceeds the dashboard's exact numeric range");
  return Number(result);
}
