export interface DryRunStep {
  step_index: number;
  action_description: string;
  command_or_api_call: string;
  predicted_before_state: Record<string, unknown>;
  predicted_after_state: Record<string, unknown>;
  reversibility: "instant" | "minutes" | "manual";
  warnings: string[];
}

export interface DryRunReport {
  problem_id: string;
  plan_action: string;
  steps: DryRunStep[];
  gemini_summary: string;
  cached: boolean;
  computed_at: string;
}

export interface AlternativeExplanation {
  explanation: string;
  evidence_for: string;
  evidence_against: string;
  likelihood: number;
}

export interface Diagnosis {
  root_cause: string;
  confidence: number;
  confidence_band: "high" | "medium" | "low";
  evidence_strength: "direct" | "circumstantial" | "speculative";
  alternative_explanations: AlternativeExplanation[];
  unknowns: string[];
  confidence_rationale: string;
}

export interface AuditEvent {
  event_id: string;
  seq: number;
  timestamp: string;
  actor: "agent" | "operator" | "system";
  actor_identity: string;
  action_type: string;
  resource?: string | null;
  incident_id?: string | null;
  payload: Record<string, unknown>;
  result: "success" | "failure" | "partial";
  hash_chain: string;
  expires_at?: string;
}

export type IncidentStatus =
  | "DETECTING"
  | "DIAGNOSING"
  | "AWAITING_APPROVAL"
  | "REMEDIATING"
  | "RESOLVED"
  | "REJECTED"
  | "PREDICTIVE";

export type RollbackSafety = "reversible" | "non-destructive" | "destructive";

export interface TraceStep {
  step: number;
  thought: string;
  tool_call?: { name: string; args: Record<string, unknown> } | null;
  tool_result?: unknown;
  timestamp: string;
  provider?: "dynatrace" | "gcp" | null;
}

export interface RemediationPlan {
  action: string;
  // Cloud Run
  service?: string | null;
  revision?: string | null;
  min_instances?: number | null;
  // Cloud SQL
  instance_id?: string | null;
  // Cloud Storage
  bucket?: string | null;
  storage_class?: string | null;
  // Pub/Sub
  subscription?: string | null;
  seek_timestamp?: string | null;
  // Safety
  reason: string;
  confidence: number;
  rollback_safe: boolean;
  rollback_safety: RollbackSafety;
  requires_explicit_confirmation: boolean;
  estimated_impact: string;
  // Cost
  estimated_hourly_cost_delta_usd?: number;
  traffic_context?: "peak" | "trough" | "normal";
  cost_optimized_alternative?: RemediationPlan | null;
}

export interface Prediction {
  prediction_id: string;
  service: string;
  created_at: string;
  expires_at: string;
  predicted_breach_in_minutes: number;
  confidence: number;
  trend_description: string;
  leading_indicator_metrics: string[];
  recommended_preemptive_action?: string | null;
  prediction_validated: boolean;
  prediction_false_positive: boolean;
  materialized_incident_id?: string | null;
}

export interface Incident {
  problem_id: string;
  status: IncidentStatus;
  severity: string;
  title: string;
  service: string;
  started_at: string;
  updated_at: string;
  trace: TraceStep[];
  plan?: RemediationPlan;
  postmortem?: string;
  correlation_id?: string;
  providers_used?: string[];
  linked_prediction_id?: string | null;
  prediction_validated?: boolean;
  cluster_id?: string | null;
  diagnosis?: Diagnosis | null;
  competing_diagnosis?: {
    diagnosis: Diagnosis;
    plan: RemediationPlan;
    note: string;
  } | null;
  confidence_blocked?: boolean;
  detection_method?: "webhook" | "polling" | "demo" | null;
  time_to_detect_ms?: number | null;
  webhook_received_at?: string | null;
  demo_scenario?: string | null;
}

export type ClusterStatus =
  | "FORMING"
  | "AWAITING_APPROVAL"
  | "EXECUTING"
  | "COMPLETE"
  | "FAILED"
  | "PARTIAL";

export interface ClusterStep {
  step_index: number;
  service: string;
  action: string;
  reason: string;
  incident_id?: string | null;
  status: "pending" | "running" | "done" | "failed";
  result?: unknown;
}

export interface IncidentCluster {
  cluster_id: string;
  member_incident_ids: string[];
  root_cause_summary: string;
  confidence: number;
  coordinated_plan: ClusterStep[];
  execution_order: string[];
  status: ClusterStatus;
  created_at: string;
  updated_at: string;
  members?: Incident[];
}
