output "vpc_self_link" {
  value = google_compute_network.vpc.self_link
}

output "subnet_self_link" {
  value = google_compute_subnetwork.private.self_link
}

output "vpc_name" {
  value = google_compute_network.vpc.name
}
