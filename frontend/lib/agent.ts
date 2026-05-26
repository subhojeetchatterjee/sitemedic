/**
 * Server-side helpers for reaching the SiteMedic agent API.
 *
 * AGENT_URL      — set at runtime by deploy.sh in the Cloud Run service config.
 * AGENT_API_KEY  — same; optional when DEMO_PUBLIC=true (agent bypasses key check).
 *
 * NEXT_PUBLIC_* variants are baked into the client bundle at build time and
 * are also readable in server-side code, so they serve as fallbacks here.
 */

export function agentUrl(): string {
  const url =
    process.env.AGENT_URL ||
    process.env.NEXT_PUBLIC_AGENT_URL ||
    "http://localhost:8080";
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

export function agentApiKey(): string {
  return (
    process.env.AGENT_API_KEY ||
    process.env.NEXT_PUBLIC_AGENT_API_KEY ||
    ""
  );
}
