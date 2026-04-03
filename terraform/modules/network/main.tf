resource "google_compute_network" "vpc" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  project                 = var.project_id
}

resource "google_compute_subnetwork" "private" {
  name                     = "${var.cluster_name}-private-subnet"
  network                  = google_compute_network.vpc.self_link
  ip_cidr_range            = "10.0.0.0/20"
  region                   = var.region
  project                  = var.project_id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.1.0.0/16"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.2.0.0/24"
  }
}

resource "google_compute_router" "nat_router" {
  name    = "${var.cluster_name}-nat-router"
  network = google_compute_network.vpc.self_link
  region  = var.region
  project = var.project_id
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.nat_router.name
  region                             = var.region
  project                            = var.project_id
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_dns_managed_zone" "googleapis" {
  name        = "${var.cluster_name}-googleapis"
  dns_name    = "googleapis.com."
  description = "Private zone to route *.googleapis.com to restricted VIP for Private Google Access"
  project     = var.project_id
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.self_link
    }
  }
}

resource "google_dns_record_set" "googleapis_a" {
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.googleapis.name
  project      = var.project_id
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "googleapis_cname" {
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  managed_zone = google_dns_managed_zone.googleapis.name
  project      = var.project_id
  rrdatas      = ["restricted.googleapis.com."]
}
