import { NextRequest, NextResponse } from "next/server";
import { adminURL, sameOrigin, unavailable } from "@/lib/server";

async function review(
  request: NextRequest,
  context: { params: Promise<{ approval_id: string }> },
) {
  if (request.method === "POST" && !sameOrigin(request))
    return NextResponse.json({ detail: "Invalid request origin" }, { status: 403 });
  const session = request.cookies.get("gas_session")?.value;
  if (!session)
    return new NextResponse(
      '<!doctype html><html><head><meta charset="utf-8"><title>Sign in to review</title></head>' +
        '<body><p><a href="/login">Sign in</a>, then open this review link again.</p></body></html>',
      { status: 401, headers: { "Content-Type": "text/html", "Cache-Control": "no-store" } },
    );
  const { approval_id } = await context.params;
  if (!/^[a-fA-F0-9-]{36}$/.test(approval_id))
    return NextResponse.json({ detail: "Invalid approval" }, { status: 400 });
  try {
    const upstream = await fetch(adminURL() + "/review/" + approval_id + request.nextUrl.search, {
      method: request.method,
      headers: {
        Authorization: "Bearer " + session,
        "Content-Type": request.headers.get("content-type") || "application/x-www-form-urlencoded",
      },
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(30000),
    });
    return new NextResponse(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") || "application/json",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
      },
    });
  } catch {
    return unavailable();
  }
}

export { review as GET, review as POST };
