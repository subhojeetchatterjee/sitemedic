"use client";

import { useEffect, useState } from "react";
import {
  collection,
  onSnapshot,
  orderBy,
  query,
  limit,
  where,
  Timestamp,
} from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { Incident, Prediction, IncidentCluster } from "@/lib/types";
import IncidentCard from "@/components/IncidentCard";
import PredictionCard from "@/components/PredictionCard";
import ClusterCard from "@/components/ClusterCard";

type Tab = "active" | "forecasted" | "resolved";

function TabButton({
  label,
  count,
  secondaryCount,
  active,
  onClick,
  accent,
}: {
  label: string;
  count: number;
  secondaryCount?: number;
  active: boolean;
  onClick: () => void;
  accent?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
        active
          ? accent ?? "bg-gray-700 text-white"
          : "bg-gray-900 text-gray-400 hover:text-gray-200 border border-gray-800 hover:border-gray-600"
      }`}
    >
      {label}
      {count > 0 && (
        <span className={`text-xs px-1.5 py-0.5 rounded-full ${
          active ? "bg-white/20" : "bg-gray-800"
        }`}>{count}</span>
      )}
      {secondaryCount != null && secondaryCount > 0 && (
        <span className={`text-xs px-1.5 py-0.5 rounded-full ${
          active ? "bg-white/10 text-white/60" : "bg-gray-800 text-gray-500"
        }`}>+{secondaryCount} rejected</span>
      )}
    </button>
  );
}

export default function HomePage() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [predictions, setPredictions] = useState<Prediction[]>([]);
  const [clusters, setClusters] = useState<IncidentCluster[]>([]);
  const [incidentsLoading, setIncidentsLoading] = useState(true);
  const [predictionsLoading, setPredictionsLoading] = useState(true);
  const [clustersLoading, setClustersLoading] = useState(true);
  const [tab, setTab] = useState<Tab>("active");

  // Live incident feed
  useEffect(() => {
    const q = query(
      collection(db, "incidents"),
      orderBy("started_at", "desc"),
      limit(50),
    );
    return onSnapshot(q, snap => {
      setIncidents(snap.docs.map(d => d.data() as Incident));
      setIncidentsLoading(false);
    }, () => setIncidentsLoading(false));
  }, []);

  // Live predictions feed — only non-expired, non-false-positive
  useEffect(() => {
    const now = Timestamp.now();
    const q = query(
      collection(db, "predictions"),
      where("expires_at", ">", now),
      where("prediction_false_positive", "==", false),
      orderBy("expires_at", "asc"),
      limit(50),
    );
    return onSnapshot(q, snap => {
      setPredictions(snap.docs.map(d => d.data() as Prediction));
      setPredictionsLoading(false);
    }, () => setPredictionsLoading(false));
  }, []);

  // Live cluster feed — non-terminal clusters
  useEffect(() => {
    const q = query(
      collection(db, "incident_clusters"),
      where("status", "in", ["FORMING", "AWAITING_APPROVAL", "EXECUTING", "PARTIAL"]),
      orderBy("created_at", "desc"),
      limit(20),
    );
    return onSnapshot(q, snap => {
      setClusters(snap.docs.map(d => d.data() as IncidentCluster));
      setClustersLoading(false);
    }, () => setClustersLoading(false));
  }, []);

  const predictiveIncidents = incidents.filter(i => i.status === "PREDICTIVE");
  const active = incidents.filter(
    i => !["RESOLVED", "REJECTED", "PREDICTIVE"].includes(i.status),
  );
  const resolved = incidents.filter(i => ["RESOLVED", "REJECTED"].includes(i.status));
  const resolvedOnly = resolved.filter(i => i.status === "RESOLVED");
  const rejected = resolved.filter(i => i.status === "REJECTED");

  // Incident IDs that belong to an active cluster — rendered as part of the cluster card
  const clusteredIncidentIds = new Set(
    clusters.flatMap(c => c.member_incident_ids),
  );

  // Standalone active incidents (not in any cluster)
  const standaloneActive = active.filter(i => !clusteredIncidentIds.has(i.problem_id));

  // Forecasted tab: merge raw predictions + PREDICTIVE incidents
  const forecasted = predictions;
  const forecastedCount = forecasted.length + predictiveIncidents.length;

  const isLoading = incidentsLoading || predictionsLoading || clustersLoading;

  return (
    <div>
      {/* Page header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-100">SiteMedic</h1>
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <span className="inline-block w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          Live
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 mb-6">
        <TabButton
          label="Active"
          count={active.length + clusters.length}
          active={tab === "active"}
          onClick={() => setTab("active")}
          accent="bg-red-900 text-red-200"
        />
        <TabButton
          label="Forecasted"
          count={forecastedCount}
          active={tab === "forecasted"}
          onClick={() => setTab("forecasted")}
          accent="bg-amber-900 text-amber-200"
        />
        <TabButton
          label="Closed"
          count={resolvedOnly.length}
          secondaryCount={rejected.length}
          active={tab === "resolved"}
          onClick={() => setTab("resolved")}
        />
      </div>

      {isLoading && (
        <div className="text-gray-500 text-sm">Connecting to Firestore…</div>
      )}

      {/* ── Active tab ── */}
      {!isLoading && tab === "active" && (
        <>
          {active.length === 0 && clusters.length === 0 ? (
            <div className="border border-dashed border-gray-800 rounded-lg p-12 text-center">
              <p className="text-gray-500">No active incidents.</p>
              <p className="text-gray-600 text-sm mt-1">
                Inject a fault into the demo app or wait for the predictor to open a forecasted incident.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {/* Clusters first */}
              {clusters.length > 0 && (
                <section className="mb-2">
                  <h2 className="text-xs font-semibold text-orange-500 uppercase tracking-wider mb-3 flex items-center gap-2">
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-orange-400" />
                    Correlated Clusters ({clusters.length})
                  </h2>
                  <div className="space-y-3">
                    {clusters.map(c => (
                      <ClusterCard key={c.cluster_id} cluster={c} />
                    ))}
                  </div>
                </section>
              )}

              {/* Standalone (unclustered) active incidents */}
              {standaloneActive.length > 0 && (
                <section>
                  {clusters.length > 0 && (
                    <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">
                      Standalone Incidents ({standaloneActive.length})
                    </h2>
                  )}
                  <div className="space-y-3">
                    {standaloneActive.map(i => (
                      <IncidentCard key={i.problem_id} incident={i} />
                    ))}
                  </div>
                </section>
              )}
            </div>
          )}
        </>
      )}

      {/* ── Forecasted tab ── */}
      {!isLoading && tab === "forecasted" && (
        <>
          {forecastedCount === 0 ? (
            <div className="border border-dashed border-amber-900 rounded-lg p-12 text-center">
              <p className="text-amber-600">No forecasted incidents.</p>
              <p className="text-gray-600 text-sm mt-2">
                The predictive loop runs every 5 minutes. Start a slow memory leak via{" "}
                <code className="text-xs bg-gray-800 px-1 rounded">POST /inject/memory</code>{" "}
                to trigger a forecast.
              </p>
            </div>
          ) : (
            <>
              {/* High-confidence PREDICTIVE incidents (already opened as incidents) */}
              {predictiveIncidents.length > 0 && (
                <section className="mb-6">
                  <h2 className="text-xs font-semibold text-amber-500 uppercase tracking-wider mb-3 flex items-center gap-2">
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                    High Confidence — Incident Opened ({predictiveIncidents.length})
                  </h2>
                  <div className="space-y-3">
                    {predictiveIncidents.map(i => (
                      <IncidentCard key={i.problem_id} incident={i} />
                    ))}
                  </div>
                </section>
              )}

              {/* Raw predictions (confidence 0.70–0.84) */}
              {forecasted.length > 0 && (
                <section>
                  <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">
                    Predictions ({forecasted.length})
                  </h2>
                  <div className="space-y-3">
                    {forecasted.map(p => (
                      <PredictionCard key={p.prediction_id} prediction={p} />
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
        </>
      )}

      {/* ── Closed tab ── */}
      {!isLoading && tab === "resolved" && (
        <>
          {resolved.length === 0 ? (
            <div className="border border-dashed border-gray-800 rounded-lg p-12 text-center">
              <p className="text-gray-500">No closed incidents yet.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {resolvedOnly.map(i => <IncidentCard key={i.problem_id} incident={i} />)}
              {rejected.length > 0 && (
                <>
                  <p className="text-xs font-semibold text-gray-600 uppercase tracking-wider pt-2">
                    Rejected ({rejected.length})
                  </p>
                  {rejected.map(i => <IncidentCard key={i.problem_id} incident={i} />)}
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
