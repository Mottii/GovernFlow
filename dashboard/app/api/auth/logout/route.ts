import { NextRequest, NextResponse } from "next/server";
import { adminURL, sameOrigin } from "@/lib/server";
export async function POST(request: NextRequest) {
  if (!sameOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin" },
      { status: 403 },
    );
  const token = request.cookies.get("gas_session")?.value;
  try {
    const upstream = await fetch(adminURL() + "/admin/auth/logout", {
      method: "POST",
      headers: { Authorization: "Bearer " + token },
      signal: AbortSignal.timeout(15000),
    });
    if (!upstream.ok && upstream.status !== 401)
      return NextResponse.json(
        { detail: "Sign out failed. Try again." },
        { status: 503 },
      );
  } catch {
    return NextResponse.json(
      { detail: "Sign out failed. Try again." },
      { status: 503 },
    );
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.delete("gas_session");
  return response;
}
