variable "app_image" {
  description = "The app image to load into the cluster. Must match the image in k8s/api.yaml and k8s/worker.yaml."
  type        = string
  default     = "lecture-platform:v4"
}

variable "keda_version" {
  description = "KEDA Helm chart version"
  type        = string
  default     = "2.20.0"
}
