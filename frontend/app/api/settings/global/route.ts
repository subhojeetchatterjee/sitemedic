import { NextRequest, NextResponse } from "next/server";
import { agentUrl, agentApiKey } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${agentUrl()}/api/settings/global`, { cache: "no-store" });
    const data = res.ok ? await res.json() : {};
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({});
  }
}

export async function PUT(req: NextRequest) {
  try {
    const body = await req.json();
    const res = await fetch(`${agentUrl()}/api/settings/global`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": agentApiKey(),
      },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
