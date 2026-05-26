/* Cloud Monitoring dashboards and alerting policies for SiteMedic agent observability. */

locals {
  agent_service_name = "sitemedic-agent-${var.environment}"
}

# ────────────────────────────────────────────────────────────────────────────
# Uptime Check for Agent Health Endpoint
# ────────────────────────────────────────────────────────────────────────────

resource "google_monitoring_uptime_check_config" "agent_health" {
  count           = var.enable_monitoring ? 1 : 0
  display_name    = "SiteMedic Agent Health Check - ${var.environment}"
  timeout         = "10s"
  period          = "60s"
  selected_regions = ["USA"]

  http_check {
    path = "/health"
    port = 8080
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      host = "${local.agent_service_name}.run.app"
    }
  }
}

# ────────────────────────────────────────────────────────────────────────────
# Agent Health Dashboard
# ────────────────────────────────────────────────────────────────────────────

resource "google_monitoring_dashboard" "agent_health" {
  count             = var.enable_monitoring ? 1 : 0
  dashboard_json    = jsonencode({
    displayName = "SiteMedic Agent Health - ${var.environment}"
    gridLayout = {
      widgets = [
        {
          title = "Agent Service Status (uptime)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" resource.type=\"uptime_url\""
                  }
                }
              }
            ]
          }
        }
        {
          title = "Cloud Run Request Count"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\" resource.label.service_name=\"${local.agent_service_name}\""
                  }
                }
              }
            ]
          }
        }
        {
          title = "Cloud Run Request Latencies (p99)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/request_latencies\" resource.type=\"cloud_run_revision\" resource.label.service_name=\"${local.agent_service_name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_99"
                    }
                  }
                }
              }
            ]
          }
        }
        {
          title = "Cloud Run CPU Usage"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/container_cpu_allocations\" resource.type=\"cloud_run_revision\" resource.label.service_name=\"${local.agent_service_name}\""
                  }
                }
              }
            ]
          }
        }
        {
          title = "Cloud Run Memory Usage"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/container_memory_allocations\" resource.type=\"cloud_run_revision\" resource.label.service_name=\"${local.agent_service_name}\""
                  }
                }
              }
            ]
          }
        }
        {
          title = "Firestore Operations (last 24h)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"firestore.googleapis.com/operation_count\""
                  }
                }
              }
            ]
          }
        }
      ]
    }
  })
}

# ────────────────────────────────────────────────────────────────────────────
# Alert Policies (basic Cloud Run metrics)
# ────────────────────────────────────────────────────────────────────────────

# Alert: Agent service down (uptime check fails)
resource "google_monitoring_alert_policy" "agent_down" {
  count           = var.enable_monitoring && length(var.alert_channels) > 0 ? 1 : 0
  display_name    = "SiteMedic Agent Down - ${var.environment}"
  combiner        = "OR"
  notification_channels = var.alert_channels

  conditions {
    display_name = "Uptime check failed"
    condition_threshold {
      filter            = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" resource.type=\"uptime_url\""
      duration          = "300s"
      comparison        = "COMPARISON_LT"
      threshold_value   = 1
      trigger_percent   = 100
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# Alert: High error rate on agent
resource "google_monitoring_alert_policy" "agent_errors" {
  count           = var.enable_monitoring && length(var.alert_channels) > 0 ? 1 : 0
  display_name    = "SiteMedic Agent: High Error Rate - ${var.environment}"
  combiner        = "OR"
  notification_channels = var.alert_channels

  conditions {
    display_name = "Cloud Run error rate > 5%"
    condition_threshold {
      filter            = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND metadata.user_labels.service_name=\"${local.agent_service_name}\""
      duration          = "300s"
      comparison        = "COMPARISON_GT"
      threshold_value   = 0.05
      trigger_percent   = 80
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# Alert: High latency on agent endpoints
resource "google_monitoring_alert_policy" "agent_latency" {
  count           = var.enable_monitoring && length(var.alert_channels) > 0 ? 1 : 0
  display_name    = "SiteMedic Agent: High Latency - ${var.environment}"
  combiner        = "OR"
  notification_channels = var.alert_channels

  conditions {
    display_name = "p99 latency > 30 seconds"
    condition_threshold {
      filter            = "metric.type=\"run.googleapis.com/request_latencies\" AND resource.type=\"cloud_run_revision\""
      duration          = "300s"
      comparison        = "COMPARISON_GT"
      threshold_value   = 30000  # milliseconds
      aggregations {
        alignment_period    = "60s"
        per_series_aligner  = "ALIGN_PERCENTILE_99"
      }
      trigger_percent   = 90
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# ────────────────────────────────────────────────────────────────────────────
# Log Sink for Structured Logs to Cloud Storage
# ────────────────────────────────────────────────────────────────────────────

resource "google_logging_project_sink" "agent_logs_export" {
  count              = var.enable_monitoring ? 1 : 0
  name               = "sitemedic-agent-logs-${var.environment}"
  destination        = "storage.googleapis.com/${google_storage_bucket.agent_logs[0].name}"
  filter             = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.agent_service_name}\""
  unique_writer_identity = true
}

resource "google_storage_bucket" "agent_logs" {
  count         = var.enable_monitoring ? 1 : 0
  name          = "sitemedic-agent-logs-${var.project_id}-${var.environment}"
  location      = var.region
  force_destroy = false

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  versioning {
    enabled = false
  }
}

resource "google_storage_bucket_iam_member" "agent_logs_sink_writer" {
  count  = var.enable_monitoring ? 1 : 0
  bucket = google_storage_bucket.agent_logs[0].name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.agent_logs_export[0].writer_identity
}
