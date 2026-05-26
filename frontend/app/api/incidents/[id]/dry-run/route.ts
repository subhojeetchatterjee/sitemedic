import { NextRequest, NextResponse } from "next/server";
import { agentUrl, agentApiKey } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function POST(
  _req: NextRequest,
  { params }: { params: { id: string } },
) {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 55_000);

    let upstream: Response;
    try {
      upstream = await fetch(
        `${agentUrl()}/api/incidents/${params.id}/dry-run`,
        {
          method: "POST",
          headers: { "X-API-Key": agentApiKey() },
          cache: "no-store",
          signal: controller.signal,
        },
      );
    } finally {
      clearTimeout(timeout);
    }

    const data = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
