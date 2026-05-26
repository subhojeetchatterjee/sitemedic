"use client";

import { useEffect, useState } from "react";

export default function DemoPublicBanner() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    // Show when the agent reports demo_public=true
    fetch("/api/demo/status", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        if (d?.demo_public === true) setShow(true);
      })
      .catch(() => {});
  }, []);

  if (!show) return null;

  return (
    <div className="bg-amber-900/40 border-b border-amber-700/50 px-6 py-2 flex items-center justify-between gap-4 text-sm">
      <span className="text-amber-200">
        <span className="font-semibold">Public demo</span>
        {" — "}no login required. Showing pre-recorded production scenarios with live Gemini reasoning.
        Approve/Reject actions are attributed to{" "}
        <span className="font-mono text-amber-300">demo-operator</span>.
      </span>
      <a
        href="/demo"
        className="shrink-0 text-amber-400 hover:text-amber-200 underline underline-offset-2"
      >
        Open Demo Panel
      </a>
    </div>
  );
}
