import { NextRequest, NextResponse } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET(
  _req: NextRequest,
  { params }: { params: { id: string } },
) {
  try {
    const res = await fetch(
      `${agentUrl()}/api/audit?incident_id=${encodeURIComponent(params.id)}&limit=200`,
      { cache: "no-store" },
    );
    const data = res.ok ? await res.json() : [];
    return NextResponse.json(data);
  } catch {
    return NextResponse.json([]);
  }
}
