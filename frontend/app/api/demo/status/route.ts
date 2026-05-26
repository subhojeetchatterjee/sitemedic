import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL || "http://localhost:8080";

export async function GET() {
  try {
    const res = await fetch(`${AGENT_URL}/api/demo/status`, {
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
    // Return a safe fallback so the frontend doesn't break if the agent is down
    return NextResponse.json(
      {
        mode: "unknown",
        source_type: "unknown",
        is_live: false,
        health_status: "unreachable",
        demo_mode_active: false,
        current_scenario: null,
        scenarios_available: 0,
        initialised: false,
        error: String(err),
      },
      { status: 200 } // return 200 so client-side code doesn't fail
    );
  }
}
