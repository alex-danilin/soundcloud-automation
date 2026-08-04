output "cloud_run_url" {
  description = "The HTTP trigger URL of the deployed Cloud Run service"
  value       = google_cloud_run_v2_service.default.uri
}

output "scheduler_job_name" {
  description = "The name of the created Cloud Scheduler job"
  value       = google_cloud_scheduler_job.hourly_sync.name
}
