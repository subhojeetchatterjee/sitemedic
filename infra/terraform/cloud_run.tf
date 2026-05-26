/* Cloud Run services for agent and frontend deployment. */

# ────────────────────────────────────────────────────────────────────────────
# Agent Service (FastAPI)
# ────────────────────────────────────────────────────────────────────────────

resource "google_cloud_run_service" "agent" {
  project  = var.gcp_project_id
  name     = "sitemedic-agent-${var.environment}"
  location = var.gcp_region

  template {
    spec {
      service_account_name = google_service_account.agent.email

      containers {
        image = "${var.gcp_region}-docker.pkg.dev/${var.gcp_project_id}/sitemedic/agent:${var.agent_image_tag}"

        ports {
          container_port = 8080
          name           = "http1"
        }

        # Resource limits
        resources {
          limits = {
            cpu    = var.agent_cpu
            memory = var.agent_memory
          }
        }

        # Inject secrets as environment variables
        env {
          name  = "ENV"
          value = var.environment
        }

        env {
          name  = "GCP_PROJECT_ID"
          value = var.gcp_project_id
        }

        env {
          name  = "GCP_REGION"
          value = var.gcp_region
        }

        # Secrets from Secret Manager
        env {
          name = "DT_API_TOKEN"
          value_from {
            secret_key_ref {
              name = "sitemedic-dynatrace-api-token-${var.environment}"
              key  = "latest"
            }
          }
        }

        env {
          name = "DT_WEBHOOK_SECRET"
          value_from {
            secret_key_ref {
              name = "sitemedic-dynatrace-webhook-secret-${var.environment}"
              key  = "latest"
            }
          }
        }

        env {
          name = "AGENT_API_KEY"
          value_from {
            secret_key_ref {
              name = "sitemedic-agent-api-key-${var.environment}"
              key  = "latest"
            }
          }
        }

        env {
          name = "SLACK_WEBHOOK_URL"
          value_from {
            secret_key_ref {
              name = "sitemedic-slack-webhook-url-${var.environment}"
              key  = "latest"
            }
          }
        }

        env {
          name = "PAGERDUTY_INTEGRATION_KEY"
          value_from {
            secret_key_ref {
              name = "sitemedic-pagerduty-integration-key-${var.environment}"
              key  = "latest"
            }
          }
        }

        # Liveness & readiness probes
        liveness_probe {
          http_get {
            path = "/health"
            port = 8080
          }
          initial_delay_seconds = 30
          timeout_seconds       = 5
          period_seconds        = 10
          failure_threshold     = 3
        }

        startup_probe {
          http_get {
            path = "/health"
            port = 8080
          }
          initial_delay_seconds = 10
          timeout_seconds       = 3
          period_seconds        = 5
          failure_threshold     = 10
        }
      }

      timeout_seconds = 900  # 15 minutes for Gemini API calls
      min_instances   = var.agent_min_instances
      max_instances   = var.agent_max_instances
    }

    metadata {
      annotations = {
        "autoscaling.knative.dev/minScale" = var.agent_min_instances
        "autoscaling.knative.dev/maxScale" = var.agent_max_instances
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }

  depends_on = [
    google_service_account_iam_member.agent_secrets_accessor,
  ]
}

# Public access for agent (via API Gateway or service-to-service)
resource "google_cloud_run_service_iam_member" "agent_invoker" {
  service            = google_cloud_run_service.agent.name
  location           = google_cloud_run_service.agent.location
  role               = "roles/run.invoker"
  member             = "allUsers"  # Frontend and other services can invoke
}

# ────────────────────────────────────────────────────────────────────────────
# Frontend Service (Next.js)
# ────────────────────────────────────────────────────────────────────────────

resource "google_cloud_run_service" "frontend" {
  project  = var.gcp_project_id
  name     = "sitemedic-frontend-${var.environment}"
  location = var.gcp_region

  template {
    spec {
      service_account_name = google_service_account.frontend.email

      containers {
        image = "${var.gcp_region}-docker.pkg.dev/${var.gcp_project_id}/sitemedic/frontend:${var.frontend_image_tag}"

        ports {
          container_port = 3000
          name           = "http1"
        }

        resources {
          limits = {
            cpu    = var.frontend_cpu
            memory = var.frontend_memory
          }
        }

        # Environment variables for Next.js
        env {
          name  = "NODE_ENV"
          value = var.environment == "prod" ? "production" : "development"
        }

        env {
          name  = "NEXT_PUBLIC_AGENT_URL"
          value = "https://${google_cloud_run_service.agent.status[0].url}"
        }

        env {
          name = "NEXT_PUBLIC_AGENT_API_KEY"
          value_from {
            secret_key_ref {
              name = "sitemedic-agent-api-key-${var.environment}"
              key  = "latest"
            }
          }
        }

        env {
          name = "NEXT_PUBLIC_FIREBASE_API_KEY"
          value_from {
            secret_key_ref {
              name = "sitemedic-firebase-api-key-${var.environment}"
              key  = "latest"
            }
          }
        }

        env {
          name = "NEXT_PUBLIC_FIREBASE_PROJECT_ID"
          value = var.gcp_project_id
        }

        # Liveness probe
        liveness_probe {
          http_get {
            path = "/"
            port = 3000
          }
          initial_delay_seconds = 20
          timeout_seconds       = 5
          period_seconds        = 10
          failure_threshold     = 3
        }

        startup_probe {
          http_get {
            path = "/"
            port = 3000
          }
          initial_delay_seconds = 10
          timeout_seconds       = 3
          period_seconds        = 5
          failure_threshold     = 10
        }
      }

      timeout_seconds = 120  # Standard for Next.js
      min_instances   = var.frontend_min_instances
      max_instances   = var.frontend_max_instances
    }

    metadata {
      annotations = {
        "autoscaling.knative.dev/minScale" = var.frontend_min_instances
        "autoscaling.knative.dev/maxScale" = var.frontend_max_instances
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }

  depends_on = [
    google_service_account_iam_member.frontend_secrets_accessor,
  ]
}

# Public access for frontend
resource "google_cloud_run_service_iam_member" "frontend_invoker" {
  service  = google_cloud_run_service.frontend.name
  location = google_cloud_run_service.frontend.location
  role     = "roles/run.invoker"
  member   = "allUsers"  # Public website
}

# ────────────────────────────────────────────────────────────────────────────
# Domain Mapping (Custom Domains)
# ────────────────────────────────────────────────────────────────────────────

# Example: map to custom domain
# Requires DNS CNAME → ghs.googlehosted.com
resource "google_cloud_run_domain_mapping" "frontend_domain" {
  count       = var.frontend_domain != "" ? 1 : 0
  project     = var.gcp_project_id
  name        = var.frontend_domain
  location    = var.gcp_region
  service_name = google_cloud_run_service.frontend.name
}

resource "google_cloud_run_domain_mapping" "agent_domain" {
  count       = var.agent_domain != "" ? 1 : 0
  project     = var.gcp_project_id
  name        = var.agent_domain
  location    = var.gcp_region
  service_name = google_cloud_run_service.agent.name
}

# ────────────────────────────────────────────────────────────────────────────
# Load Balancer with Cloud Armor (optional)
# ────────────────────────────────────────────────────────────────────────────

resource "google_compute_security_policy" "policy" {
  count   = var.enable_cloud_armor ? 1 : 0
  name    = "sitemedic-${var.environment}"
  project = var.gcp_project_id

  # Allow all traffic by default
  rules {
    action   = "allow"
    priority = "65535"
    match {
      versioned_expr = "EXPR_V1"
      expr {
        expression = "true"
      }
    }
    description = "Default rule"
  }

  # Example: Rate limit per IP
  rules {
    action   = "rate_based_ban"
    priority = "100"
    match {
      versioned_expr = "EXPR_V1"
      expr {
        expression = "true"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      rate_limit_key = "IP"
      ban_duration_sec = 600
    }
    description = "Rate limit: 100 req/min per IP"
  }
}

# ────────────────────────────────────────────────────────────────────────────
# Outputs
# ────────────────────────────────────────────────────────────────────────────

output "agent_url" {
  description = "Agent service URL"
  value       = google_cloud_run_service.agent.status[0].url
}

output "frontend_url" {
  description = "Frontend service URL"
  value       = google_cloud_run_service.frontend.status[0].url
}

output "agent_domain_mapping" {
  description = "Agent domain mapping status"
  value       = var.agent_domain != "" ? google_cloud_run_domain_mapping.agent_domain[0].status[0].resource_records : null
}

output "frontend_domain_mapping" {
  description = "Frontend domain mapping status"
  value       = var.frontend_domain != "" ? google_cloud_run_domain_mapping.frontend_domain[0].status[0].resource_records : null
}
