from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, Field


class AlternativeExplanation(BaseModel):
    explanation: str
    evidence_for: str
    evidence_against: str
    likelihood: float = Field(ge=0.0, le=1.0)


class Diagnosis(BaseModel):
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_band: Literal["high", "medium", "low"]
    evidence_strength: Literal["direct", "circumstantial", "speculative"]
    alternative_explanations: List[AlternativeExplanation] = Field(min_length=2)
    unknowns: List[str]
    confidence_rationale: str


class RemediationAction(str, Enum):
    # Cloud Run
    rollback_revision            = "rollback_revision"
    scale_service                = "scale_service"
    restart_service              = "restart_service"
    no_action_needed             = "no_action_needed"
    # Cloud SQL
    failover_cloud_sql_replica   = "failover_cloud_sql_replica"
    restart_cloud_sql_instance   = "restart_cloud_sql_instance"
    # Cloud Storage
    change_bucket_storage_class  = "change_bucket_storage_class"
    enable_bucket_versioning     = "enable_bucket_versioning"
    # Pub/Sub
    purge_pubsub_subscription_backlog  = "purge_pubsub_subscription_backlog"
    seek_subscription_to_timestamp     = "seek_subscription_to_timestamp"


RollbackSafety = Literal["reversible", "non-destructive", "destructive"]

# Actions that are never reversible — require typed resource-ID confirmation before execution
DESTRUCTIVE_ACTIONS = {RemediationAction.purge_pubsub_subscription_backlog}


class TraceStep(BaseModel):
    step: int
    thought: str
    tool_call: Optional[dict[str, Any]] = None
    tool_result: Optional[Any] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # Which observability provider supplied this tool call's data
    provider: Optional[str] = None  # "dynatrace" | "gcp" | None (thought-only steps)


class RemediationPlan(BaseModel):
    action: RemediationAction

    # ── Cloud Run params ───────────────────────────────────────────────────
    service: Optional[str] = None
    revision: Optional[str] = None
    min_instances: Optional[int] = None

    # ── Cloud SQL params ───────────────────────────────────────────────────
    instance_id: Optional[str] = None          # Cloud SQL instance name

    # ── Cloud Storage params ───────────────────────────────────────────────
    bucket: Optional[str] = None               # bucket name (no gs:// prefix)
    storage_class: Optional[str] = None        # STANDARD | NEARLINE | COLDLINE | ARCHIVE

    # ── Pub/Sub params ────────────────────────────────────────────────────
    subscription: Optional[str] = None         # projects/{p}/subscriptions/{s}
    seek_timestamp: Optional[str] = None       # ISO-8601 for seek_subscription_to_timestamp

    # ── Safety metadata ────────────────────────────────────────────────────
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    rollback_safe: bool                        # kept for backwards compat with existing UI
    rollback_safety: RollbackSafety = "reversible"
    requires_explicit_confirmation: bool = False
    estimated_impact: str

    # ── Cost metadata (filled server-side after Gemini outputs the plan) ───
    estimated_hourly_cost_delta_usd: float = 0.0
    traffic_context: Literal["peak", "trough", "normal"] = "normal"
    # Gemini may propose a second, cheaper plan alongside the primary one
    cost_optimized_alternative: Optional["RemediationPlan"] = None


# Required for Pydantic v2 self-referential model
RemediationPlan.model_rebuild()


class Prediction(BaseModel):
    prediction_id: str
    service: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime                        # created_at + 30 minutes
    predicted_breach_in_minutes: int = Field(ge=5, le=15)
    confidence: float = Field(ge=0.0, le=1.0)
    trend_description: str
    leading_indicator_metrics: list[str]
    recommended_preemptive_action: Optional[RemediationAction] = None
    # Feedback tags — filled once the prediction window closes
    prediction_validated: bool = False          # Dynatrace confirmed the breach
    prediction_false_positive: bool = False     # window expired, no breach
    materialized_incident_id: Optional[str] = None  # links to incidents/{id}
    # Raw snapshot stored for analytics dashboard
    raw_metrics: dict[str, Any] = {}


class Incident(BaseModel):
    problem_id: str
    status: str  # DETECTING | DIAGNOSING | AWAITING_APPROVAL | REMEDIATING | RESOLVED | REJECTED | PREDICTIVE
    severity: str
    title: str
    service: str
    started_at: datetime
    trace: list[TraceStep] = []
    plan: Optional[RemediationPlan] = None
    postmortem: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Tracks which observability providers contributed during diagnosis
    correlation_id: Optional[str] = None  # set once; same value logged to both DT and GCP
    providers_used: list[str] = []        # populated as tools are called, e.g. ["dynatrace", "gcp"]
    # Prediction linkage — set when this incident was preceded by a prediction
    linked_prediction_id: Optional[str] = None
    prediction_validated: bool = False
    # Cluster linkage — set when this incident is grouped into a correlation cluster
    cluster_id: Optional[str] = None
    # Structured diagnosis with confidence self-assessment
    diagnosis: Optional[Diagnosis] = None
    competing_diagnosis: Optional[dict] = None  # set by self-consistency check
    confidence_blocked: bool = False  # true when confidence < 0.6 (auto-action blocked)
    # Detection method and latency
    detection_method: Optional[Literal["webhook", "polling"]] = None
    time_to_detect_ms: Optional[int] = None   # millis from DT problem open to Firestore write
    webhook_received_at: Optional[datetime] = None


class ClusterStep(BaseModel):
    step_index: int
    service: str
    action: RemediationAction
    reason: str
    incident_id: Optional[str] = None
    status: Literal["pending", "running", "done", "failed"] = "pending"
    result: Optional[Any] = None


class IncidentCluster(BaseModel):
    cluster_id: str
    member_incident_ids: list[str]
    root_cause_summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    coordinated_plan: list[ClusterStep] = []
    execution_order: list[str] = []
    status: Literal["FORMING", "AWAITING_APPROVAL", "EXECUTING", "COMPLETE", "FAILED", "PARTIAL"] = "AWAITING_APPROVAL"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClusterApprovalDecision(BaseModel):
    mode: Literal["all_at_once", "step_by_step"] = "all_at_once"
    rejected: bool = False
    rejected_reason: Optional[str] = None


class DryRunStep(BaseModel):
    step_index: int
    action_description: str
    command_or_api_call: str
    predicted_before_state: dict
    predicted_after_state: dict
    reversibility: Literal["instant", "minutes", "manual"]
    warnings: List[str]


class DryRunReport(BaseModel):
    problem_id: str
    plan_action: str
    steps: List[DryRunStep]
    gemini_summary: str
    cached: bool = False
    computed_at: str


class ApprovalDecision(BaseModel):
    approved: bool
    rejected_reason: Optional[str] = None
    # Operator must type the primary resource identifier for destructive actions
    # (e.g. the subscription name for purge_pubsub_subscription_backlog)
    explicit_confirmation: Optional[str] = None
    # When True: simulate approval without executing (same as /dry-run endpoint)
    dry_run: bool = False


class DetectRequest(BaseModel):
    """Manually trigger a detection cycle (useful for testing)."""
    force: bool = False
