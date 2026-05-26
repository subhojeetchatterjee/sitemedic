/* Data cleanup scheduler using Cloud Tasks and Cloud Scheduler. */

locals {
  cleanup_handler_url = "https://sitemedic-agent-${var.environment}.run.app/api/maintenance/cleanup"
}

# ────────────────────────────────────────────────────────────────────────────
# Cloud Scheduler Job for Daily Cleanup
# ────────────────────────────────────────────────────────────────────────────

resource "google_cloud_scheduler_job" "daily_cleanup" {
  project             = var.gcp_project_id
  name                = "sitemedic-daily-cleanup-${var.environment}"
  description         = "Daily Firestore data cleanup (delete stale incidents, predictions, etc.)"
  schedule            = "0 2 * * *"  # 2 AM UTC daily
  time_zone           = "UTC"
  attempt_deadline    = "320s"
  region              = var.region
  paused              = false

  http_target {
    uri        = local.cleanup_handler_url
    http_method = "POST"
    headers = {
      "X-API-Key" = var.agent_api_key
      "Content-Type" = "application/json"
    }

    body = base64encode(jsonencode({
      action = "cleanup_all"
      dry_run = false
      environment = var.environment
    }))

    oidc_token {
      service_account_email = google_service_account.agent.email
    }
  }

  depends_on = [google_cloud_scheduler_job.enable_cloud_scheduler_api]
}

# Enable Cloud Scheduler API (required)
resource "null_resource" "enable_cloud_scheduler_api" {
  triggers = {
    project_id = var.gcp_project_id
  }

  provisioner "local-exec" {
    command = "gcloud services enable cloudscheduler.googleapis.com --project=${var.gcp_project_id}"
  }
}

# Reference to make Terraform happy
resource "google_cloud_scheduler_job" "enable_cloud_scheduler_api" {
  project = var.gcp_project_id
  name    = "dummy"
  schedule = "0 0 * * *"

  http_target {
    uri = "https://example.com"
    http_method = "GET"
  }

  provisioner "local-exec" {
    command = "gcloud services enable cloudscheduler.googleapis.com --project=${var.gcp_project_id}"
  }
}

# ────────────────────────────────────────────────────────────────────────────
# Logs for Cleanup Runs
# ────────────────────────────────────────────────────────────────────────────

# Query cleanup logs:
# gcloud logging read \
#   'resource.type="cloud_run_revision" AND jsonPayload.action="cleanup_all"' \
#   --limit 50 --format json

resource "google_logging_log_sink" "cleanup_logs" {
  name        = "sitemedic-cleanup-logs-${var.environment}"
  destination = "logging.googleapis.com"

  filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"sitemedic-agent-${var.environment}\" AND jsonPayload.action=\"cleanup_all\""

  unique_writer_identity = false
}

# ────────────────────────────────────────────────────────────────────────────
# Alert on Cleanup Failures
# ────────────────────────────────────────────────────────────────────────────

resource "google_monitoring_alert_policy" "cleanup_failure" {
  count           = length(var.alert_channels) > 0 ? 1 : 0
  display_name    = "SiteMedic: Cleanup Job Failed - ${var.environment}"
  combiner        = "OR"
  notification_channels = var.alert_channels

  conditions {
    display_name = "Cleanup job error"
    condition_threshold {
      filter            = "resource.type=\"cloud_scheduler_job\" AND jsonPayload.status=\"FAILED\" AND resource.labels.job_name=\"sitemedic-daily-cleanup-${var.environment}\""
      duration          = "300s"
      comparison        = "COMPARISON_GT"
      threshold_value   = 0
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
}
