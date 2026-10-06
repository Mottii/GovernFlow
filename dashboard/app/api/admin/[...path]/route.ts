import { NextRequest, NextResponse } from "next/server";
import { adminURL, sameOrigin, unavailable } from "@/lib/server";
async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  if (request.method !== "GET" && !sameOrigin(request))
    return NextResponse.json(
      { detail: "Invalid request origin" },
      { status: 403 },
    );
  const token = request.cookies.get("gas_session")?.value;
  if (!token)
    return NextResponse.json(
      { detail: "Sign in to continue" },
      { status: 401 },
    );
  const { path } = await context.params;
  if (path.some((p) => !/^[a-zA-Z0-9_-]+$/.test(p)))
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  try {
    const upstream = await fetch(
      adminURL() + "/admin/" + path.join("/") + request.nextUrl.search,
      {
        method: request.method,
        headers: {
          Authorization: "Bearer " + token,
          "Content-Type": "application/json",
        },
        body: ["GET", "HEAD"].includes(request.method)
          ? undefined
          : await request.text(),
        cache: "no-store",
        signal: AbortSignal.timeout(30000),
      },
    );
    return new NextResponse(
      upstream.status === 204 ? null : await upstream.arrayBuffer(),
      {
        status: upstream.status,
        headers: {
          "Content-Type":
            upstream.headers.get("content-type") || "application/json",
          "Cache-Control": "no-store",
          ...(upstream.headers.get("content-disposition")
            ? {
                "Content-Disposition": upstream.headers.get(
                  "content-disposition",
                )!,
              }
            : {}),
        },
      },
    );
  } catch {
    return unavailable();
  }
}
export { proxy as GET, proxy as POST, proxy as DELETE, proxy as PATCH };
