import { NextRequest, NextResponse } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  try {
    const { searchParams } = new URL(req.url);
    const params = new URLSearchParams();
    for (const [k, v] of searchParams.entries()) params.set(k, v);
    if (!params.has("limit")) params.set("limit", "200");

    const res = await fetch(
      `${agentUrl()}/api/audit?${params.toString()}`,
      { cache: "no-store" },
    );
    const data = res.ok ? await res.json() : [];
    return NextResponse.json(data);
  } catch {
    return NextResponse.json([]);
  }
}
