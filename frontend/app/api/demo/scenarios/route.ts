import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL || "http://localhost:8080";

export async function GET() {
  try {
    const res = await fetch(`${AGENT_URL}/api/demo/scenarios`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      return NextResponse.json(
        { error: `Agent returned ${res.status}` },
        { status: res.status }
      );
    }
    const data = await res.json();
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: `Failed to reach agent: ${String(err)}` },
      { status: 502 }
    );
  }
}
