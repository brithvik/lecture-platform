output "next_steps" {
  value = <<-EOT
    Platform is up. Check it with:  kubectl get pods -A
    Open the app with:              kubectl port-forward svc/api 8000:8000
  EOT
}
