module "bastion" {
  source  = "terraform-google-modules/bastion-host/google"
  version = "~> 6.0"

  project    = var.project_id
  network    = var.network
  subnet     = var.subnetwork
  name       = "${var.cluster_name}-bastion"
  zone       = var.zone != "" ? var.zone : "${var.region}-b"

  machine_type   = "e2-micro"
  image_project  = "debian-cloud"
  image_family   = "debian-12"
  shielded_vm    = true

  startup_script = file("${path.module}/startup_script.sh")
  members = var.members
}
