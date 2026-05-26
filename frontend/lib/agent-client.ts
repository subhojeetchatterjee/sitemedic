/**
 * Centralized agent API client configuration.
 *
 * Ensures NEXT_PUBLIC_AGENT_URL is properly set and fails clearly if missing.
 */

function getAgentUrl(): string {
  const url = process.env.NEXT_PUBLIC_AGENT_URL;

  if (!url) {
    throw new Error(
      "NEXT_PUBLIC_AGENT_URL environment variable is not set. " +
      "Set it to the agent API URL (e.g., http://localhost:8080 for dev, https://sitemedic-agent-prod.run.app for prod)"
    );
  }

  return url.endsWith("/") ? url.slice(0, -1) : url;
}

// Cache the URL on first access
let cachedAgentUrl: string | null = null;

export function getAgent(): string {
  if (cachedAgentUrl === null) {
    cachedAgentUrl = getAgentUrl();
  }
  return cachedAgentUrl;
}

/**
 * Make a request to the agent API.
 * Automatically includes error handling and proper Content-Type headers.
 */
export async function fetchAgent(
  path: string,
  options?: RequestInit
): Promise<Response> {
  const agent = getAgent();
  const url = `${agent}${path.startsWith("/") ? path : "/" + path}`;

  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
    ...options,
  });

  if (!response.ok) {
    throw new Error(`Agent API error: ${response.status} ${response.statusText}`);
  }

  return response;
}

/**
 * Example usage in components:
 *
 * import { getAgent, fetchAgent } from "@/lib/agent-client";
 *
 * // Method 1: Manual URL building
 * const agent = getAgent();
 * const res = await fetch(`${agent}/api/incidents`);
 *
 * // Method 2: Using helper
 * const res = await fetchAgent("/api/incidents");
 * const data = await res.json();
 */
