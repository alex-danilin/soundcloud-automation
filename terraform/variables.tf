variable "project_id" {
  type        = string
  description = "GCP Project ID"
}

variable "region" {
  type        = string
  description = "GCP Region for Cloud Run & Scheduler"
  default     = "us-central1"
}

variable "service_name" {
  type        = string
  description = "Name of the Cloud Run service"
  default     = "soundcloud-auto-playlist"
}

# SoundCloud Secrets
variable "soundcloud_client_id" {
  type        = string
  description = "SoundCloud OAuth Client ID"
  sensitive   = true
}

variable "soundcloud_client_secret" {
  type        = string
  description = "SoundCloud OAuth Client Secret"
  sensitive   = true
}

variable "soundcloud_refresh_token" {
  type        = string
  description = "SoundCloud OAuth Refresh Token"
  sensitive   = true
}

# Telegram Secrets
variable "telegram_bot_token" {
  type        = string
  description = "Telegram Bot Token from @BotFather"
  sensitive   = true
}

variable "telegram_chat_id" {
  type        = string
  description = "Telegram Chat ID"
  sensitive   = true
}

# Application Config
variable "lookback_minutes" {
  type        = string
  description = "Lookback window in minutes for liked tracks"
  default     = "65"

  validation {
    condition     = can(regex("^[0-9]+$", var.lookback_minutes)) && tonumber(var.lookback_minutes) >= 1 && tonumber(var.lookback_minutes) <= 10080
    error_message = "The lookback_minutes variable must be a numeric string between 1 and 10080."
  }
}

variable "playlist_prefix" {
  type        = string
  description = "Prefix for created genre playlists"
  default     = "Genre: "
}

variable "default_genre" {
  type        = string
  description = "Default fallback genre"
  default     = "Uncategorized"
}

variable "playlist_sharing" {
  type        = string
  description = "Privacy setting for auto-created playlists (private or public)"
  default     = "private"
}
