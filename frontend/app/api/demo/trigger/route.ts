import { NextRequest, NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL || "http://localhost:8080";
const AGENT_API_KEY = process.env.AGENT_API_KEY || "";

export async function POST(req: NextRequest) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  // set_speed is handled by a separate agent endpoint
  const endpoint = body.set_speed !== undefined
    ? `${AGENT_URL}/api/demo/speed`
    : `${AGENT_URL}/api/demo/run`;

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": AGENT_API_KEY,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });

    const data = await res.json();
    if (!res.ok) {
      return NextResponse.json(
        { error: data.detail || data.error || `Agent returned ${res.status}` },
        { status: res.status }
      );
    }
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: `Failed to reach agent: ${String(err)}` },
      { status: 502 }
    );
  }
}
