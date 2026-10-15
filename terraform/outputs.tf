output "cluster_name" {
  value = kind_cluster.this.name
}

output "next_steps" {
  value = <<-EOT
    Cluster is up. Check it with:   kubectl get pods -A
    Open the app with:              kubectl port-forward svc/api 8000:8000
    Tear everything down with:      terraform destroy
  EOT
}
