terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "network" {
  source       = "./modules/network"
  project_id   = var.project_id
  region       = var.region
  cluster_name = var.cluster_name
}

module "bastion" {
  source       = "./modules/bastion"
  project_id   = var.project_id
  region       = var.region
  zone         = var.bastion_zone
  cluster_name = var.cluster_name
  network      = module.network.vpc_self_link
  subnetwork   = module.network.subnet_self_link
  members      = var.bastion_members
}

module "cluster" {
  source           = "./modules/cluster"
  project_id       = var.project_id
  region           = var.region
  cluster_name     = var.cluster_name
  vpc_self_link    = module.network.vpc_self_link
  subnet_self_link = module.network.subnet_self_link
  bastion_ip       = module.bastion.ip_address
}

module "storage" {
  source     = "./modules/storage"
  project_id = var.project_id
  region     = var.region
}

module "iam" {
  source          = "./modules/iam"
  project_id      = var.project_id
  region          = var.region
  raw_data_bucket = module.storage.raw_data_bucket_name
  iceberg_bucket  = module.storage.iceberg_bucket_name
  ray_ksa_name    = var.ray_ksa_name
  ray_namespace   = var.ray_namespace
  cluster_name    = var.cluster_name
}
