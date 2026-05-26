"use client";

import { useEffect, useState } from "react";

const COOKIE_NAME = "sitemedic_demo_banner_dismissed";
const STATUS_KEY = "sitemedic_source_status";

function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

function setCookie(name: string, value: string, days: number) {
  const expires = new Date(Date.now() + days * 864e5).toUTCString();
  document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/`;
}

export default function DemoWelcomeBanner() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    // Only show if demo mode is active AND user hasn't dismissed
    if (getCookie(COOKIE_NAME) === "true") return;

    try {
      const cached = localStorage.getItem(STATUS_KEY);
      if (cached) {
        const status = JSON.parse(cached);
        if (status.demo_mode_active || status.mode === "demo") {
          setShow(true);
        }
      }
    } catch {}

    // Also check live status
    fetch("/api/demo/status", { cache: "no-store" })
      .then((r) => r.json())
      .then((s) => {
        if (s && (s.demo_mode_active || s.mode === "demo")) {
          if (getCookie(COOKIE_NAME) !== "true") {
            setShow(true);
          }
        }
      })
      .catch(() => {});
  }, []);

  function dismiss() {
    setCookie(COOKIE_NAME, "true", 7); // dismisses for 7 days
    setShow(false);
  }

  if (!show) return null;

  return (
    <div className="bg-amber-950 border-b border-amber-800 px-6 py-3 flex items-center justify-between gap-4">
      <div className="flex items-center gap-3 min-w-0">
        <span className="text-amber-400 shrink-0 text-lg" aria-hidden>🎬</span>
        <p className="text-sm text-amber-200">
          <span className="font-semibold">You&apos;re viewing SiteMedic in demo mode.</span>{" "}
          Real Gemini reasoning is happening live against pre-recorded production scenarios —
          no Dynatrace tenant required.{" "}
          <a href="/demo" className="underline hover:text-amber-100">
            Open demo control panel →
          </a>
        </p>
      </div>
      <button
        onClick={dismiss}
        className="shrink-0 text-amber-400 hover:text-amber-200 text-sm px-2 py-1"
        aria-label="Dismiss demo banner"
      >
        Dismiss
      </button>
    </div>
  );
}
