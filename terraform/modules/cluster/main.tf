resource "google_container_cluster" "main" {
  name       = var.cluster_name
  location   = var.region
  network    = var.vpc_self_link
  subnetwork = var.subnet_self_link
  project    = var.project_id

  deletion_protection      = true
  remove_default_node_pool = true
  initial_node_count = 1
  node_locations = ["asia-south1-b"]

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes = true
    enable_private_endpoint = false
    master_ipv4_cidr_block = "172.16.0.0/28"
  }

  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = "${var.bastion_ip}/32"
      display_name = "bastion"
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  network_policy {
    enabled  = true
    provider = "CALICO"
  }

  release_channel {
    channel = "UNSPECIFIED"
  }

  cluster_autoscaling {
    enabled = false
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = true
    }
  }
}

resource "google_container_node_pool" "head" {
  name     = "head-pool"
  cluster  = google_container_cluster.main.name
  location = var.region
  project  = var.project_id

  initial_node_count = 1
  autoscaling {
    min_node_count = 1
    max_node_count = 3
  }

  management {
    auto_repair  = true
    auto_upgrade = false
  }

  node_config {
    machine_type = "e2-standard-2"
    disk_size_gb = 30
    disk_type    = "pd-balanced"
    image_type   = "COS_CONTAINERD"

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    labels = {
      pool = "head-pool"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}

resource "google_container_node_pool" "train" {
  name           = "train-pool"
  cluster        = google_container_cluster.main.name
  location       = var.region
  project        = var.project_id
  node_locations = ["asia-south1-b"]

  autoscaling {
    min_node_count = 0
    max_node_count = 2
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  management {
    auto_repair  = true
    auto_upgrade = false
  }

  node_config {
    machine_type = "n1-standard-8"
    disk_size_gb = 25
    disk_type    = "pd-ssd"
    image_type   = "COS_CONTAINERD"

    guest_accelerator {
      type  = "nvidia-tesla-t4"
      count = 1
      gpu_driver_installation_config {
        gpu_driver_version = "DEFAULT"
      }
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    labels = {
      pool = "train-pool"
      "nvidia.com/device-plugin.config" = "any"
    }

    taint {
      key    = "ray-train"
      value  = "true"
      effect = "NO_SCHEDULE"
    }

    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}

resource "google_container_node_pool" "worker" {
  name     = "worker-pool"
  cluster  = google_container_cluster.main.name
  location = var.region
  project  = var.project_id

  initial_node_count = 1
  autoscaling {
    min_node_count = 1
    max_node_count = 10
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  management {
    auto_repair  = true
    auto_upgrade = false
  }

  node_config {
    machine_type = "e2-standard-2"
    disk_size_gb = 20
    disk_type    = "pd-balanced"
    image_type   = "COS_CONTAINERD"
    spot         = true

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    labels = {
      pool = "worker-pool"
    }

    taint {
      key    = "cloud.google.com/gke-spot"
      value  = "true"
      effect = "NO_SCHEDULE"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}
