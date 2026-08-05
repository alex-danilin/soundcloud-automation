terraform {
  required_version = ">= 1.3.0"

  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
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
    "scheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com"
  ])
  service            = each.key
  disable_on_destroy = false
}

# 2. Artifact Registry Repository & Container Build (C-3)
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = var.service_name
  format        = "DOCKER"
  description   = "Docker repository for SoundCloud Automation Cloud Run service"
  depends_on    = [google_project_service.apis]
}

resource "null_resource" "build_push" {
  triggers = {
    src_hash = sha1(join("", [for f in fileset("${path.module}/..", "*.py") : filesha1("${path.module}/../${f}")]))
  }

  provisioner "local-exec" {
    command = "gcloud builds submit ${path.module}/.. --tag ${var.region}-docker.pkg.dev/${var.project_id}/${var.service_name}/app:latest --project ${var.project_id}"
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository.repo
  ]
}

# 3. Dedicated Service Accounts (C-2, M-9)
resource "google_service_account" "runtime_sa" {
  account_id   = "${var.service_name}-runtime-sa"
  display_name = "SoundCloud Bot Runtime Service Account"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "invoker_sa" {
  account_id   = "${var.service_name}-invoker-sa"
  display_name = "Cloud Scheduler Invoker Service Account"
  depends_on   = [google_project_service.apis]
}

# 4. GCS State Bucket for persistent state (Section 6)
resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-${var.service_name}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "state_rw" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime_sa.email}"
}

# 5. Secret Manager Resources
locals {
  static_secrets_map = {
    "soundcloud-client-id"     = var.soundcloud_client_id
    "soundcloud-client-secret" = var.soundcloud_client_secret
    "telegram-bot-token"       = var.telegram_bot_token
    "telegram-chat-id"         = var.telegram_chat_id
  }

  all_secret_ids = [
    "soundcloud-client-id",
    "soundcloud-client-secret",
    "soundcloud-refresh-token",
    "telegram-bot-token",
    "telegram-chat-id"
  ]
}

# Create Secret containers for all secrets
resource "google_secret_manager_secret" "all_secrets" {
  for_each  = toset(local.all_secret_ids)
  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Standard secret versions (updates when tfvars changes)
resource "google_secret_manager_secret_version" "static_versions" {
  for_each    = local.static_secrets_map
  secret      = google_secret_manager_secret.all_secrets[each.key].id
  secret_data = each.value
}

# H-4 FIX: Scoped lifecycle ignore_changes ONLY for the auto-rotating refresh token
resource "google_secret_manager_secret_version" "refresh_token_version" {
  secret      = google_secret_manager_secret.all_secrets["soundcloud-refresh-token"].id
  secret_data = var.soundcloud_refresh_token

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# Grant Runtime SA Secret Manager Access (C-2, C-4)
resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = toset(local.all_secret_ids)
  secret_id = google_secret_manager_secret.all_secrets[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "refresh_token_version_adder" {
  secret_id = google_secret_manager_secret.all_secrets["soundcloud-refresh-token"].secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = "serviceAccount:${google_service_account.runtime_sa.email}"
}

# 6. Cloud Run v2 Service
resource "google_cloud_run_v2_service" "default" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.runtime_sa.email
    timeout         = "600s"

    scaling {
      max_instance_count = 2
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${var.service_name}/app:latest"

      ports {
        container_port = 8080
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }
        initial_delay_seconds = 5
        timeout_seconds       = 3
        period_seconds        = 5
        failure_threshold     = 3
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "STATE_BUCKET"
        value = google_storage_bucket.state.name
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
      env {
        name  = "PLAYLIST_SHARING"
        value = var.playlist_sharing
      }

      # Secret environment variables mounted from Secret Manager
      env {
        name = "SOUNDCLOUD_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.all_secrets["soundcloud-client-id"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOUNDCLOUD_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.all_secrets["soundcloud-client-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOUNDCLOUD_REFRESH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.all_secrets["soundcloud-refresh-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "TELEGRAM_BOT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.all_secrets["telegram-bot-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "TELEGRAM_CHAT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.all_secrets["telegram-chat-id"].secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.static_versions,
    google_secret_manager_secret_version.refresh_token_version,
    google_secret_manager_secret_iam_member.accessor,
    null_resource.build_push
  ]
}

# 7. IAM for Cloud Scheduler Invocation
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  location = google_cloud_run_v2_service.default.location
  name     = google_cloud_run_v2_service.default.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker_sa.email}"
}

# 8. Cloud Scheduler Job (Hourly Authenticated Trigger)
resource "google_cloud_scheduler_job" "hourly_sync" {
  name             = "${var.service_name}-hourly-sync"
  description      = "Hourly trigger for SoundCloud auto-playlist sync"
  schedule         = "0 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  http_target {
    http_method = "GET"
    uri         = google_cloud_run_v2_service.default.uri

    oidc_token {
      service_account_email = google_service_account.invoker_sa.email
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_service.default,
    google_service_account.invoker_sa
  ]
}
