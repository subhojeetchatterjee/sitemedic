import { NextRequest, NextResponse } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const window = req.nextUrl.searchParams.get("window") ?? "30d";
  if (!["7d", "30d", "90d"].includes(window)) {
    return NextResponse.json({ error: "Invalid window" }, { status: 400 });
  }

  try {
    const upstream = await fetch(
      `${agentUrl()}/api/analytics?window=${window}`,
      { next: { revalidate: 300 } },
    );
    if (!upstream.ok) {
      return NextResponse.json(
        { error: `Agent returned ${upstream.status}` },
        { status: upstream.status },
      );
    }
    const data = await upstream.json();
    return NextResponse.json(data, {
      headers: { "Cache-Control": "public, max-age=300, stale-while-revalidate=60" },
    });
  } catch {
    return NextResponse.json(null, { status: 200 });
  }
}
