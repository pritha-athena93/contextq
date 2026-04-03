resource "google_service_account" "ray_sa" {
  account_id   = "ray-sa"
  display_name = "Ray Pipeline Service Account"
  project      = var.project_id
}

resource "google_storage_bucket_iam_member" "ray_raw_data" {
  bucket = var.raw_data_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ray_sa.email}"
}

resource "google_storage_bucket_iam_member" "ray_iceberg" {
  bucket = var.iceberg_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ray_sa.email}"
}

resource "google_project_iam_member" "ray_sa_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.ray_sa.email}"
}

resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.ray_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.ray_namespace}/${var.ray_ksa_name}]"
}
