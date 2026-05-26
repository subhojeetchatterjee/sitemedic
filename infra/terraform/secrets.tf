/**
 * Google Secret Manager resources for SiteMedic
 *
 * Stores all credentials, API tokens, and secrets.
 * Access is controlled via IAM bindings to specific service accounts.
 */

# Dynatrace API Token (per environment)
resource "google_secret_manager_secret" "dynatrace_api_token" {
  secret_id = "${local.prefix}-dynatrace-api-token-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "dynatrace"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "dynatrace_api_token" {
  secret      = google_secret_manager_secret.dynatrace_api_token.id
  secret_data = var.dynatrace_api_token
}

# Dynatrace Webhook Signing Secret (per environment)
resource "google_secret_manager_secret" "dynatrace_webhook_secret" {
  secret_id = "${local.prefix}-dynatrace-webhook-secret-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "dynatrace"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "dynatrace_webhook_secret" {
  secret      = google_secret_manager_secret.dynatrace_webhook_secret.id
  secret_data = var.dynatrace_webhook_secret
}

# Agent API Key (for frontend to authenticate with agent)
resource "google_secret_manager_secret" "agent_api_key" {
  secret_id = "${local.prefix}-agent-api-key-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "agent"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "agent_api_key" {
  secret      = google_secret_manager_secret.agent_api_key.id
  secret_data = var.agent_api_key
}

# Firebase Admin SDK credentials (JSON)
resource "google_secret_manager_secret" "firebase_admin_credentials" {
  secret_id = "${local.prefix}-firebase-admin-credentials-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "firebase"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "firebase_admin_credentials" {
  secret      = google_secret_manager_secret.firebase_admin_credentials.id
  secret_data = var.firebase_admin_credentials_json
}

# Slack webhook URL (for notifications)
resource "google_secret_manager_secret" "slack_webhook_url" {
  secret_id = "${local.prefix}-slack-webhook-url-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "slack"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "slack_webhook_url" {
  secret      = google_secret_manager_secret.slack_webhook_url.id
  secret_data = var.slack_webhook_url
}

# PagerDuty integration key (for on-call alerts)
resource "google_secret_manager_secret" "pagerduty_integration_key" {
  secret_id = "${local.prefix}-pagerduty-integration-key-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "pagerduty"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "pagerduty_integration_key" {
  secret      = google_secret_manager_secret.pagerduty_integration_key.id
  secret_data = var.pagerduty_integration_key
}

# Google Chat webhook URL (for notifications)
resource "google_secret_manager_secret" "google_chat_webhook_url" {
  secret_id = "${local.prefix}-google-chat-webhook-url-${var.environment}"

  labels = {
    app     = local.prefix
    env     = var.environment
    service = "google-chat"
  }

  replication {
    automatic = true
  }
}

resource "google_secret_manager_secret_version" "google_chat_webhook_url" {
  secret      = google_secret_manager_secret.google_chat_webhook_url.id
  secret_data = var.google_chat_webhook_url
}

# Outputs for use by Cloud Run services
output "secrets" {
  description = "Secret Manager secret IDs and versions"
  value = {
    dynatrace_api_token         = google_secret_manager_secret.dynatrace_api_token.id
    dynatrace_webhook_secret    = google_secret_manager_secret.dynatrace_webhook_secret.id
    agent_api_key               = google_secret_manager_secret.agent_api_key.id
    firebase_admin_credentials  = google_secret_manager_secret.firebase_admin_credentials.id
    slack_webhook_url           = google_secret_manager_secret.slack_webhook_url.id
    pagerduty_integration_key   = google_secret_manager_secret.pagerduty_integration_key.id
    google_chat_webhook_url     = google_secret_manager_secret.google_chat_webhook_url.id
  }
}
