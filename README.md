# SoundCloud Auto Follow & Genre Playlist Bot

[![GCP Cloud Run](https://img.shields.io/badge/GCP-Cloud%20Run-blue?logo=googlecloud)](https://cloud.google.com/run)
[![Terraform](https://img.shields.io/badge/Terraform-1.3+-623CE4?logo=terraform)](https://www.terraform.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)](https://python.org)

Production-ready automation microservice deployed on **Google Cloud Run** via **Terraform**. When triggered periodically via GCP Cloud Scheduler, it checks your recently liked tracks on SoundCloud, automatically follows the track artist, organizes the track into a genre-specific playlist (creating the playlist if missing), and notifies you via a **Telegram Bot** with track links, genre, and detected musical key.

---

## 🚀 Features

- **Artist Auto-Follow:** Automatically follows the creator of any SoundCloud track you like (`PUT /me/followings/{artist_id}`).
- **Genre Playlist Auto-Sorter:** Automatically creates or appends tracks to genre playlists (e.g., `Genre: Techno`, `Genre: Deep House`) while preserving authoritative playlist contents.
- **Musical Key Detection:** Parses Camelot wheel keys (e.g., `8A`, `1B`) and standard key signatures (e.g., `F#m`, `C Major`, `Am`) from track tags, title, and description.
- **Telegram Bot Notifications:** Formats an HTML notification message containing direct SoundCloud links (`permalink_url`), artist, genre, key signature, and tri-state follow status.
- **Persistent State & Deduplication:** Tracks last-processed likes and notification history via a GCS state bucket (`state.json`) and Secret Manager OAuth token rotation.
- **Container Hardening:** Runs as non-root user (`appuser` UID 10001) with bounded dependency constraints.
- **Infrastructure as Code (Terraform):** Terraform provisions GCP APIs, Secret Manager secrets, IAM service accounts, Artifact Registry docker repositories, GCS state buckets, Cloud Run v2 service, and Cloud Scheduler triggers.

---

## 🛠️ Architecture Overview

```
 [ GCP Cloud Scheduler ]
          │ (Cron OIDC HTTP Trigger every 1 hr)
          ▼
  [ Cloud Run Service ] ◄── (Provisioned & Managed by Terraform)
          │
          ├──► [ GCS State Bucket ]  ──► (Persist last processed like ID & FIFO notified ledger)
          ├──► [ Secret Manager ]    ──► (Auto-rotate OAuth Refresh Token)
          ├──► [ SoundCloud API ]    ──► (Fetch Recent Likes, Follow Artist, Sync Genre Playlist)
          └──► [ Telegram Bot API ]  ──► (Post Notification Message to Chat)
```

---

## 📋 Prerequisites

1. **Google Cloud Platform (GCP) Account** with `gcloud` CLI installed and authenticated.
2. **Terraform CLI** (v1.3 or higher) installed ([Download Terraform](https://developer.hashicorp.com/terraform/downloads)).
3. **SoundCloud Application & OAuth Credentials**:
   - `Client ID` & `Client Secret` from [SoundCloud Developer Portal](https://developers.soundcloud.com/docs/building-with-ai).
   - `Refresh Token` obtained via OAuth 2.0 flow.
4. **Telegram Bot Token & Chat ID**:
   - Create a bot via [@BotFather](https://t.me/BotFather) to get your `TELEGRAM_BOT_TOKEN`.
   - Send a message to your bot and get your `TELEGRAM_CHAT_ID` via `https://api.telegram.org/bot<TOKEN>/getUpdates`.

---

## 🏗️ Deployment via Terraform

Deployment is managed via **Terraform**. Ensure your `gcloud` CLI is logged in so Terraform can submit Cloud Build container builds.

### 1. Authenticate with GCP

Run these commands from **Windows PowerShell**:

```powershell
gcloud auth login
gcloud auth application-default login
```

### 2. Configure Variables

Navigate to the `terraform/` directory and copy `terraform.tfvars.example` to `terraform.tfvars`:

```powershell
cd terraform
Copy-Item terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your GCP Project ID and API credentials:

```hcl
project_id               = "your-gcp-project-id"
region                   = "us-central1"
service_name             = "soundcloud-auto-playlist"

soundcloud_client_id     = "your_soundcloud_client_id"
soundcloud_client_secret = "your_soundcloud_client_secret"
soundcloud_refresh_token = "your_soundcloud_refresh_token"

telegram_bot_token       = "your_telegram_bot_token"
telegram_chat_id         = "your_telegram_chat_id"

lookback_minutes         = "65"
playlist_prefix          = "Genre: "
default_genre            = "Uncategorized"
playlist_sharing         = "private"
```

### 3. Apply Terraform Infrastructure

Deploy the entire stack with Terraform:

```powershell
# Initialize Terraform providers
terraform init

# Preview resource creation
terraform plan

# Deploy infrastructure
terraform apply -auto-approve
```

### What Terraform Provisions Automatically:

1. **GCP Service Enablement:** Enables `run`, `secretmanager`, `cloudbuild`, `artifactregistry`, `storage`, and `scheduler` APIs.
2. **Artifact Registry & Build:** Provisions Docker repository and builds/pushes the microservice image via Cloud Build.
3. **GCP Secret Manager:** Secures SoundCloud & Telegram credentials as secret versions with rotation lifecycle management.
4. **GCS State Bucket:** Provisions state storage for exact resume boundaries and notified track ledgers.
5. **IAM Service Accounts:** Provisions runtime service accounts with least-privilege `secretAccessor`, `secretVersionAdder`, and `objectAdmin` permissions.
6. **Cloud Run v2 Service:** Deploys the Python microservice container in `us-central1` with secrets securely mounted.
7. **Cloud Scheduler Job:** Creates an hourly cron job (`0 * * * *`) authenticating via OIDC service account token.

---

## 💡 Suggested Features & Roadmap for Open Source

Looking to contribute or enhance this repository? Here are great feature ideas:

1. **AI Musical Key & Genre Enrichment (Gemini AI):** Integrate Gemini 2.5 Flash to analyze track titles and descriptions to predict missing genres or extract harmonic keys when unlisted in tags.
2. **Harmonic Key Mixing & BPM Sorting:** Group tracks inside playlists by BPM ranges (e.g., `120-125 BPM`) or Camelot key compatible mixing pairs.
3. **Multi-Platform Notifications:** Add support for Discord webhooks or Slack channels alongside Telegram.
4. **Auto-Unlike Cleanup Scheduler:** Option to auto-unlike tracks after X days to keep your liked tracks feed fresh.

---

## 📜 License

This project is licensed under the **MIT License** — see the [`LICENSE`](LICENSE) file for details.
