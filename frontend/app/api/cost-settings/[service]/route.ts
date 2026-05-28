import { NextRequest, NextResponse } from "next/server";
import { agentUrl, agentApiKey } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function PUT(req: NextRequest, { params }: { params: { service: string } }) {
  try {
    const body = await req.json();
    const res = await fetch(`${agentUrl()}/api/cost-settings/${params.service}`, {
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
