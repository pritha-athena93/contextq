output "ip_address" {
  description = "Internal IP of the bastion VM — passed to cluster module for master_authorized_networks"
  value       = module.bastion.ip_address
}

output "ssh_command" {
  description = "Open an IAP SSH tunnel and start the tinyproxy forward"
  value       = "gcloud compute ssh ${module.bastion.hostname} --tunnel-through-iap --zone=${var.region}-a --project=${var.project_id} -- -L8888:localhost:8888 -N"
}

output "kubectl_alias" {
  description = "Use this prefix for all kubectl/helm commands via the bastion proxy"
  value       = "HTTPS_PROXY=localhost:8888 kubectl"
}
