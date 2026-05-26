"use client";

import ReactMarkdown from "react-markdown";

export default function Postmortem({ markdown }: { markdown: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none">
      <ReactMarkdown>{markdown}</ReactMarkdown>
    </div>
  );
}
