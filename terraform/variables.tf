variable "project_id" {
  description = "GCP project ID"
  type        = string
  default     = "gen-ai-pritha"
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "asia-south1"
}

variable "cluster_name" {
  description = "GKE cluster name"
  type        = string
  default     = "ray-platform"
}

variable "ray_ksa_name" {
  description = "Kubernetes Service Account name for Ray pods"
  type        = string
  default     = "ray-ksa"
}

variable "ray_namespace" {
  description = "Kubernetes namespace for Ray workloads"
  type        = string
  default     = "ray"
}

variable "bastion_zone" {
  description = "Zone for the bastion VM. Defaults to region-b if not set. Override to avoid zone capacity errors."
  type        = string
  default     = ""
}

variable "bastion_members" {
  description = "Identities allowed to SSH to the bastion via IAP. E.g. [\"user:you@example.com\"]"
  type        = list(string)
  default     = ["user:prithat398@gmail.com"]
}

variable "switch" {
  description = "Switch - blue/green"
  type        = string
  default     = ""
}
