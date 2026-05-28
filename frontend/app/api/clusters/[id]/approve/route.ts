import { NextRequest, NextResponse } from "next/server";
import { agentUrl, agentApiKey } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest, { params }: { params: { id: string } }) {
  try {
    const body = await req.json();
    const res = await fetch(`${agentUrl()}/api/clusters/${params.id}/approve`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": agentApiKey(),
      },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
