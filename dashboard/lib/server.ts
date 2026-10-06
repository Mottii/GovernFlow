import { NextRequest, NextResponse } from "next/server";
export const adminURL = () =>
  process.env.ADMIN_API_URL || "http://localhost:8001";
export function sameOrigin(request: NextRequest) {
  const origin = request.headers.get("origin");
  const allowed =
    process.env.DASHBOARD_PUBLIC_URL || "http://localhost:3000";
  return origin === allowed;
}
export function unavailable() {
  return NextResponse.json(
    { detail: "The admin service is unavailable. Try again." },
    { status: 503 },
  );
}
