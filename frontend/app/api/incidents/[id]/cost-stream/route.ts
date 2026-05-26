import { NextRequest } from "next/server";
import { agentUrl } from "@/lib/agent";

export const dynamic = "force-dynamic";

export async function GET(
  _req: NextRequest,
  { params }: { params: { id: string } },
) {
  try {
    const upstream = await fetch(
      `${agentUrl()}/api/incidents/${params.id}/cost-stream`,
      {
        headers: { Accept: "text/event-stream" },
        cache: "no-store",
      },
    );

    if (!upstream.ok || !upstream.body) {
      return new Response(`data: {"error":"upstream ${upstream.status}"}\n\n`, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    return new Response(upstream.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  } catch {
    return new Response(`data: {"error":"agent unreachable"}\n\n`, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }
}
