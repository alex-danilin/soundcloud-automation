terraform {
  required_version = ">= 1.3.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required GCP APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "scheduler.googleapis.com"
  ])
  service            = each.key
  disable_on_destroy = false
}

# 2. Secret Manager Resources
locals {
  secrets_map = {
    "soundcloud-client-id"     = var.soundcloud_client_id
    "soundcloud-client-secret" = var.soundcloud_client_secret
    "soundcloud-refresh-token" = var.soundcloud_refresh_token
    "telegram-bot-token"        = var.telegram_bot_token
    "telegram-chat-id"          = var.telegram_chat_id
  }
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = local.secrets_map
  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "versions" {
  for_each    = local.secrets_map
  secret      = google_secret_manager_secret.secrets[each.key].id
  secret_data = each.value
}

# 3. Cloud Run v2 Service
resource "google_cloud_run_v2_service" "default" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    containers {
      image = "gcr.io/${var.project_id}/${var.service_name}:latest"

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      env {
        name  = "LOOKBACK_MINUTES"
        value = var.lookback_minutes
      }
      env {
        name  = "PLAYLIST_PREFIX"
        value = var.playlist_prefix
      }
      env {
        name  = "DEFAULT_GENRE"
        value = var.default_genre
      }

      # Secret environment variables mounted from Secret Manager
      env {
        name = "SOUNDCLOUD_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["soundcloud-client-id"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOUNDCLOUD_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["soundcloud-client-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOUNDCLOUD_REFRESH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["soundcloud-refresh-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "TELEGRAM_BOT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["telegram-bot-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "TELEGRAM_CHAT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["telegram-chat-id"].secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.versions
  ]
}

# 4. Allow Unauthenticated Access (Public Invoker for HTTP Trigger)
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  location = google_cloud_run_v2_service.default.location
  name     = google_cloud_run_v2_service.default.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# 5. Cloud Scheduler Job (Hourly Trigger)
resource "google_cloud_scheduler_job" "hourly_sync" {
  name             = "${var.service_name}-hourly-sync"
  description      = "Hourly trigger for SoundCloud auto-playlist sync"
  schedule         = "0 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "180s"

  http_target {
    http_method = "GET"
    uri         = google_cloud_run_v2_service.default.uri
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_service.default
  ]
}
