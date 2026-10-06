import { NextRequest, NextResponse } from "next/server";
import { adminURL, sameOrigin, unavailable } from "@/lib/server";
export async function POST(request: NextRequest) {
  if (!sameOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin" },
      { status: 403 },
    );
  try {
    const upstream = await fetch(adminURL() + "/admin/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: await request.text(),
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
    const data = await upstream.json();
    if (!upstream.ok)
      return NextResponse.json(data, { status: upstream.status });
    const response = NextResponse.json({ role: data.role });
    response.cookies.set("gas_session", data.token, {
      httpOnly: true,
      sameSite: "strict",
      secure:
        new URL(process.env.DASHBOARD_PUBLIC_URL || request.url).protocol ===
        "https:",
      path: "/",
      maxAge: data.expires_in,
    });
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch {
    return unavailable();
  }
}
