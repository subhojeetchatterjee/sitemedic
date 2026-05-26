import { NextResponse } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${agentUrl()}/api/system-health`, { cache: "no-store" });
    const data = res.ok ? await res.json() : {};
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({}, { status: 200 });
  }
}
