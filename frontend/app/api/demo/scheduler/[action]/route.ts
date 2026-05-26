import { NextRequest, NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL || "http://localhost:8080";
const AGENT_API_KEY = process.env.AGENT_API_KEY || "";

export async function POST(
  _req: NextRequest,
  { params }: { params: { action: string } }
) {
  const action = params.action;
  if (action !== "pause" && action !== "resume") {
    return NextResponse.json({ error: "Action must be pause or resume" }, { status: 400 });
  }

  try {
    const res = await fetch(`${AGENT_URL}/api/demo/scheduler/${action}`, {
      method: "POST",
      headers: { "X-API-Key": AGENT_API_KEY },
      signal: AbortSignal.timeout(10000),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return NextResponse.json(
        { error: data.detail || `Agent returned ${res.status}` },
        { status: res.status }
      );
    }
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
