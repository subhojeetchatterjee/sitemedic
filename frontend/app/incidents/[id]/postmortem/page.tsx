"use client";

import { useEffect, useState } from "react";
import { doc, getDoc } from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { Incident } from "@/lib/types";
import Postmortem from "@/components/Postmortem";
import Link from "next/link";

export default function PostmortemPage({ params }: { params: { id: string } }) {
  const [incident, setIncident] = useState<Incident | null>(null);

  useEffect(() => {
    getDoc(doc(db, "incidents", params.id)).then(snap => {
      if (snap.exists()) setIncident(snap.data() as Incident);
    });
  }, [params.id]);

  if (!incident) return <div className="text-gray-500 text-sm">Loading…</div>;

  return (
    <div className="max-w-3xl">
      <Link href={`/incidents/${params.id}`} className="text-sm text-gray-500 hover:text-gray-300 mb-4 inline-block">
        ← Back to incident
      </Link>
      {incident.postmortem ? (
        <div className="border border-gray-800 rounded-lg p-6 bg-gray-900">
          <Postmortem markdown={incident.postmortem} />
        </div>
      ) : (
        <p className="text-gray-500 text-sm">Postmortem not yet generated.</p>
      )}
    </div>
  );
}
