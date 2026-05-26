terraform {
  required_version = ">= 1.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # Backend configuration for remote state (Cloud Storage with state locking)
  # Uncomment and configure after initial setup:
  # backend "gcs" {
  #   bucket = "sitemedic-terraform-state"
  #   prefix = "sitemedic"
  # }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

variable "gcp_project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "gcp_region" {
  description = "GCP region for resources"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

locals {
  project_id = var.gcp_project_id
  region     = var.gcp_region
  environment = var.environment

  # Naming convention
  prefix = "sitemedic"

  # Common labels for all resources
  common_labels = {
    project     = "sitemedic"
    environment = var.environment
    managed_by  = "terraform"
    created_at  = timestamp()
  }
}

# Data source: current GCP account
data "google_client_config" "current" {}

output "project_id" {
  value = local.project_id
}

output "region" {
  value = local.region
}

output "environment" {
  value = local.environment
}
