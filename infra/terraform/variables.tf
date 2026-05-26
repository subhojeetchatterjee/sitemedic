/**
 * Terraform input variables for SiteMedic infrastructure
 *
 * Usage:
 *   terraform plan -var-file=dev.tfvars
 *   terraform plan -var-file=staging.tfvars
 *   terraform plan -var-file=prod.tfvars
 */

# Required variables (no defaults)
variable "dynatrace_api_token" {
  description = "Dynatrace API token for problem detection"
  type        = string
  sensitive   = true
}

variable "dynatrace_webhook_secret" {
  description = "Webhook signing secret (HMAC-SHA256)"
  type        = string
  sensitive   = true
}

variable "agent_api_key" {
  description = "API key for frontend to authenticate with agent"
  type        = string
  sensitive   = true
}

variable "firebase_admin_credentials_json" {
  description = "Firebase admin SDK service account JSON"
  type        = string
  sensitive   = true
}

variable "slack_webhook_url" {
  description = "Slack webhook URL for notifications (optional, empty string if not configured)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "pagerduty_integration_key" {
  description = "PagerDuty integration key for alerts (optional, empty string if not configured)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_chat_webhook_url" {
  description = "Google Chat webhook URL for notifications (optional, empty string if not configured)"
  type        = string
  sensitive   = true
  default     = ""
}

# Provider variables
variable "gcp_project_id" {
  description = "GCP project ID"
  type        = string
}

variable "gcp_region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

# Environment
variable "environment" {
  description = "Deployment environment: dev, staging, or prod"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be one of: dev, staging, prod"
  }
}

# Monitoring and alerting
variable "alert_channels" {
  description = "List of notification channels for Cloud Monitoring alerts (e.g., email, Slack)"
  type        = list(string)
  default     = []  # No alerts by default; configure in tfvars
}

variable "enable_monitoring" {
  description = "Whether to create monitoring dashboards and alert policies"
  type        = bool
  default     = true
}

# Firestore & Data Hygiene
variable "firestore_backup_retention_days" {
  description = "Number of days to retain Firestore backups (production only)"
  type        = number
  default     = 30
}

# Cloud Run - Agent Service
variable "agent_image_tag" {
  description = "Docker image tag for agent service"
  type        = string
  default     = "latest"
}

variable "agent_cpu" {
  description = "CPU allocation for agent service"
  type        = string
  default     = "2"
}

variable "agent_memory" {
  description = "Memory allocation for agent service"
  type        = string
  default     = "2Gi"
}

variable "agent_min_instances" {
  description = "Minimum instances for agent service"
  type        = number
  default     = 1
}

variable "agent_max_instances" {
  description = "Maximum instances for agent service"
  type        = number
  default     = 10
}

# Cloud Run - Frontend Service
variable "frontend_image_tag" {
  description = "Docker image tag for frontend service"
  type        = string
  default     = "latest"
}

variable "frontend_cpu" {
  description = "CPU allocation for frontend service"
  type        = string
  default     = "1"
}

variable "frontend_memory" {
  description = "Memory allocation for frontend service"
  type        = string
  default     = "1Gi"
}

variable "frontend_min_instances" {
  description = "Minimum instances for frontend service"
  type        = number
  default     = 1
}

variable "frontend_max_instances" {
  description = "Maximum instances for frontend service"
  type        = number
  default     = 5
}

# Custom domains
variable "agent_domain" {
  description = "Custom domain for agent service (e.g., agent.example.com)"
  type        = string
  default     = ""
}

variable "frontend_domain" {
  description = "Custom domain for frontend service (e.g., sitemedic.example.com)"
  type        = string
  default     = ""
}

# Security
variable "enable_cloud_armor" {
  description = "Enable Cloud Armor DDoS protection and rate limiting"
  type        = bool
  default     = false  # Enable in production
}
