/* Firestore database configuration, indexes, and backup policies. */

locals {
  firestore_database = var.environment == "prod" ? "(default)" : var.environment
}

# ────────────────────────────────────────────────────────────────────────────
# Firestore Database
# ────────────────────────────────────────────────────────────────────────────

resource "google_firestore_database" "main" {
  project                         = var.gcp_project_id
  name                            = local.firestore_database
  location_id                     = var.region
  type                            = "FIRESTORE_NATIVE"
  delete_protection_enabled       = var.environment == "prod"  # Prevent accidental deletion in prod
  point_in_time_recovery_enabled  = var.environment == "prod"  # Enable PITR in prod

  depends_on = [google_app_engine_application.main]
}

# App Engine required for Firestore
resource "google_app_engine_application" "main" {
  project       = var.gcp_project_id
  location_id   = var.region
  database_type = "CLOUD_FIRESTORE"
}

# ────────────────────────────────────────────────────────────────────────────
# Composite Indexes (required for complex queries)
# ────────────────────────────────────────────────────────────────────────────

# Index for predictions: expires_at ASC + prediction_false_positive ASC
resource "google_firestore_index" "predictions_expiry_status" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "predictions"

  fields {
    field_path = "expires_at"
    order      = "ASCENDING"
  }

  fields {
    field_path = "prediction_false_positive"
    order      = "ASCENDING"
  }

  query_scope = "COLLECTION"
}

# Index for predictions: service ASC + expires_at DESC + prediction_validated ASC
resource "google_firestore_index" "predictions_service_expiry" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "predictions"

  fields {
    field_path = "service"
    order      = "ASCENDING"
  }

  fields {
    field_path = "expires_at"
    order      = "DESCENDING"
  }

  fields {
    field_path = "prediction_validated"
    order      = "ASCENDING"
  }

  query_scope = "COLLECTION"
}

# Index for incident_clusters: status ASC + created_at DESC
resource "google_firestore_index" "clusters_status_created" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "incident_clusters"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }

  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }

  query_scope = "COLLECTION"
}

# Index for audit_events: incident_id ASC + seq DESC
resource "google_firestore_index" "audit_incident_seq" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "audit_events"

  fields {
    field_path = "incident_id"
    order      = "ASCENDING"
  }

  fields {
    field_path = "seq"
    order      = "DESCENDING"
  }

  query_scope = "COLLECTION"
}

# Index for audit_events: actor ASC + seq DESC
resource "google_firestore_index" "audit_actor_seq" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "audit_events"

  fields {
    field_path = "actor"
    order      = "ASCENDING"
  }

  fields {
    field_path = "seq"
    order      = "DESCENDING"
  }

  query_scope = "COLLECTION"
}

# Index for audit_events: action_type ASC + seq DESC
resource "google_firestore_index" "audit_action_seq" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "audit_events"

  fields {
    field_path = "action_type"
    order      = "ASCENDING"
  }

  fields {
    field_path = "seq"
    order      = "DESCENDING"
  }

  query_scope = "COLLECTION"
}

# ────────────────────────────────────────────────────────────────────────────
# TTL Policies (automatic document deletion)
# ────────────────────────────────────────────────────────────────────────────

# Note: TTL policies are managed via Google Cloud Console or gcloud CLI as of now
# (Terraform support is limited). Document the TTL policy requirements:
#
# To enable TTL for audit_events:
#   gcloud firestore databases patch DATABASE --ttl-policy='{"field": "expires_at"}'
#
# For audit_events, set expires_at = created_at + 365 days (production)

# ────────────────────────────────────────────────────────────────────────────
# Backup & Recovery Configuration
# ────────────────────────────────────────────────────────────────────────────

resource "google_firestore_backup_schedule" "daily_backup" {
  project            = var.gcp_project_id
  database           = google_firestore_database.main.name
  location           = var.region
  display_name       = "Daily backup - ${var.environment}"
  retention_duration = "${var.firestore_backup_retention_days}d"

  daily_recurrence {}

  # Only create backups for production
  count = var.environment == "prod" ? 1 : 0
}

# ────────────────────────────────────────────────────────────────────────────
# Collection Management & Retention Defaults
# ────────────────────────────────────────────────────────────────────────────

# Document the following retention policies (to be enforced via Cloud Tasks scheduled cleanup):
#
# predictions:
#   - Expire after 30 minutes (via expires_at field + TTL policy)
#   - Automatic cleanup every 5 minutes
#
# incidents:
#   - RESOLVED incidents: delete after 90 days
#   - REJECTED incidents: delete after 30 days
#   - Other statuses: keep indefinitely (until resolved/rejected)
#
# incident_clusters:
#   - COMPLETE/FAILED: delete after 30 days
#   - PARTIAL: delete after 60 days
#
# audit_events:
#   - Keep for 365 days (production)
#   - Keep for 90 days (staging)
#   - Keep for 30 days (development)
#   - Auto-delete via TTL field (expires_at)
#
# analytics_snapshots:
#   - Keep all historical snapshots (never delete)
#   - Quarterly aggregation/rollup to summary tables
#
# webhook_health:
#   - Keep last 7 days of probes
#   - Delete older entries
#
# webhook_failures:
#   - Keep for 30 days
#   - Auto-archive to Cloud Storage after 7 days
