import { NextResponse } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${agentUrl()}/api/audit/verify`, { cache: "no-store" });
    const data = res.ok ? await res.json() : null;
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
