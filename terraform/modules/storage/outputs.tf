output "raw_data_bucket_name" {
  value = google_storage_bucket.raw_data.name
}

output "iceberg_bucket_name" {
  value = google_storage_bucket.iceberg_warehouse.name
}
