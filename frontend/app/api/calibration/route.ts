import { NextResponse } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const upstream = await fetch(`${agentUrl()}/api/calibration`, {
      next: { revalidate: 3600 },
    });
    if (!upstream.ok) return NextResponse.json(null, { status: 200 });
    const data = await upstream.json();
    return NextResponse.json(data, {
      headers: { "Cache-Control": "public, max-age=3600, stale-while-revalidate=300" },
    });
  } catch {
    return NextResponse.json(null, { status: 200 });
  }
}
