variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "vpc_self_link" {
  type = string
}

variable "subnet_self_link" {
  type = string
}

variable "bastion_ip" {
  description = "Internal IP of the bastion VM — whitelisted as the sole master authorized network"
  type        = string
}

variable "local_ip" {
  description = "Public IP of the local machine for direct kubectl access when bastion is unavailable"
  type        = string
  default     = ""
}
