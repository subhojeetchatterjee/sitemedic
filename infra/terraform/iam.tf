/**
 * IAM service accounts and role bindings for SiteMedic
 *
 * Principle of least privilege: each service account has only the minimum
 * permissions needed to perform its function. No Editor or Owner roles.
 */

# ─────────────────────────────────────────────────────────────────────────────
# Service Accounts
# ─────────────────────────────────────────────────────────────────────────────

# Agent service account: runs the core diagnosis and remediation logic
resource "google_service_account" "agent" {
  account_id   = "${local.prefix}-agent-${var.environment}"
  display_name = "SiteMedic Agent (${var.environment})"
  description  = "Service account for SiteMedic autonomous agent"
}

# Frontend service account: read-only access to Firestore for dashboard
resource "google_service_account" "frontend" {
  account_id   = "${local.prefix}-frontend-${var.environment}"
  display_name = "SiteMedic Frontend (${var.environment})"
  description  = "Service account for SiteMedic frontend dashboard"
}

# Webhook receiver service account: validates signatures and creates incidents
resource "google_service_account" "webhook" {
  account_id   = "${local.prefix}-webhook-${var.environment}"
  display_name = "SiteMedic Webhook Receiver (${var.environment})"
  description  = "Service account for Dynatrace webhook endpoint"
}

# ─────────────────────────────────────────────────────────────────────────────
# Custom IAM Roles (for fine-grained permission control)
# ─────────────────────────────────────────────────────────────────────────────

# Agent custom role: minimal permissions for diagnosis and remediation
resource "google_project_iam_custom_role" "agent_role" {
  role_id     = "${replace(local.prefix, "-", "_")}_agent_${replace(var.environment, "-", "_")}"
  title       = "SiteMedic Agent (${var.environment})"
  description = "Custom role for SiteMedic agent with minimal required permissions"

  permissions = [
    # Firestore: read/write incidents, audit logs, predictions, clusters
    "datastore.databases.get",
    "datastore.databases.list",
    "datastore.databases.update",
    "datastore.entities.create",
    "datastore.entities.delete",
    "datastore.entities.get",
    "datastore.entities.list",
    "datastore.entities.update",
    "datastore.indexes.create",
    "datastore.indexes.delete",
    "datastore.indexes.get",
    "datastore.indexes.list",
    "datastore.indexes.update",

    # Secret Manager: read secrets (API tokens, webhook secrets)
    "secretmanager.secrets.get",
    "secretmanager.secrets.list",
    "secretmanager.versions.access",
    "secretmanager.versions.list",

    # Vertex AI: call Gemini for diagnosis and planning
    "aiplatform.endpoints.predict",

    # Logging: write structured logs
    "logging.logEntries.create",

    # Monitoring: write custom metrics
    "monitoring.metricDescriptors.create",
    "monitoring.metricDescriptors.list",
    "monitoring.timeSeries.create",

    # Cloud Run: query service status, scale instances (for remediation)
    "run.operations.get",
    "run.operations.list",
    "run.services.get",
    "run.services.list",
    "run.services.update",  # For scaling/restart
    "run.locations.list",

    # Pub/Sub: publish notifications, manage subscriptions
    "pubsub.subscriptions.consume",
    "pubsub.subscriptions.get",
    "pubsub.subscriptions.list",
    "pubsub.subscriptions.update",
    "pubsub.topics.get",
    "pubsub.topics.list",
    "pubsub.topics.publish",

    # Cloud SQL: query databases (if needed for remediation)
    "cloudsql.instances.get",
    "cloudsql.instances.list",

    # Cloud Storage: read/write for backups and reports
    "storage.buckets.get",
    "storage.buckets.list",
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
  ]
}

# Frontend custom role: read-only access to Firestore
resource "google_project_iam_custom_role" "frontend_role" {
  role_id     = "${replace(local.prefix, "-", "_")}_frontend_${replace(var.environment, "-", "_")}"
  title       = "SiteMedic Frontend (${var.environment})"
  description = "Custom role for SiteMedic frontend with read-only access"

  permissions = [
    # Firestore: read incidents, audit logs, predictions, analytics snapshots
    "datastore.databases.get",
    "datastore.entities.get",
    "datastore.entities.list",

    # Logging: read audit logs
    "logging.logEntries.list",

    # Monitoring: read metrics for dashboards
    "monitoring.timeSeries.list",
  ]
}

# Webhook custom role: minimal permissions to receive and create incidents
resource "google_project_iam_custom_role" "webhook_role" {
  role_id     = "${replace(local.prefix, "-", "_")}_webhook_${replace(var.environment, "-", "_")}"
  title       = "SiteMedic Webhook Receiver (${var.environment})"
  description = "Custom role for Dynatrace webhook endpoint"

  permissions = [
    # Firestore: write incidents, audit logs
    "datastore.entities.create",
    "datastore.entities.update",
    "datastore.entities.list",

    # Secret Manager: read webhook secret for signature validation
    "secretmanager.secrets.get",
    "secretmanager.versions.access",

    # Logging: write webhook events
    "logging.logEntries.create",
  ]
}

# ─────────────────────────────────────────────────────────────────────────────
# IAM Bindings: Assign custom roles to service accounts
# ─────────────────────────────────────────────────────────────────────────────

resource "google_project_iam_member" "agent_custom_role" {
  project = local.project_id
  role    = google_project_iam_custom_role.agent_role.id
  member  = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_project_iam_member" "frontend_custom_role" {
  project = local.project_id
  role    = google_project_iam_custom_role.frontend_role.id
  member  = "serviceAccount:${google_service_account.frontend.email}"
}

resource "google_project_iam_member" "webhook_custom_role" {
  project = local.project_id
  role    = google_project_iam_custom_role.webhook_role.id
  member  = "serviceAccount:${google_service_account.webhook.email}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Secret Manager Access Control
# ─────────────────────────────────────────────────────────────────────────────

# Agent can read all secrets
resource "google_secret_manager_secret_iam_member" "agent_secrets" {
  for_each = toset([
    google_secret_manager_secret.dynatrace_api_token.id,
    google_secret_manager_secret.dynatrace_webhook_secret.id,
    google_secret_manager_secret.firebase_admin_credentials.id,
    google_secret_manager_secret.slack_webhook_url.id,
    google_secret_manager_secret.pagerduty_integration_key.id,
  ])

  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"
}

# Frontend can read agent API key only
resource "google_secret_manager_secret_iam_member" "frontend_agent_api_key" {
  secret_id = google_secret_manager_secret.agent_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.frontend.email}"
}

# Webhook can read Dynatrace webhook secret only
resource "google_secret_manager_secret_iam_member" "webhook_dynatrace_secret" {
  secret_id = google_secret_manager_secret.dynatrace_webhook_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.webhook.email}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Workload Identity Federation (preferred over service account keys)
# ─────────────────────────────────────────────────────────────────────────────

# Note: Workload identity is configured via Cloud Run service account bindings
# (see cloud_run.tf). Service account keys should NOT be created or used in production.

# ─────────────────────────────────────────────────────────────────────────────
# Outputs: Service account information for deployment
# ─────────────────────────────────────────────────────────────────────────────

output "service_accounts" {
  description = "SiteMedic service account emails"
  value = {
    agent    = google_service_account.agent.email
    frontend = google_service_account.frontend.email
    webhook  = google_service_account.webhook.email
  }
}

output "iam_roles" {
  description = "Custom IAM roles"
  value = {
    agent_role    = google_project_iam_custom_role.agent_role.id
    frontend_role = google_project_iam_custom_role.frontend_role.id
    webhook_role  = google_project_iam_custom_role.webhook_role.id
  }
}
