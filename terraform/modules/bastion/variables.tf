variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "asia-south1"
}

variable "zone" {
  description = "Zone for the bastion VM. Defaults to region-b."
  type        = string
  default     = ""
}

variable "network" {
  description = "Self-link of the VPC network"
  type        = string
}

variable "subnetwork" {
  description = "Self-link of the private subnet"
  type        = string
}

variable "cluster_name" {
  type = string
}

variable "members" {
  description = "List of identities allowed to SSH via IAP. E.g. [\"user:alice@example.com\"]"
  type        = list(string)
  default     = []
}
