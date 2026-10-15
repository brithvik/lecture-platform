# Which Terraform plugins ("providers") this project uses.
# Exact versions get recorded in .terraform.lock.hcl by `terraform init`; commit that file.
terraform {
  required_version = ">= 1.6"

  required_providers {
    kind = {
      source  = "tehcyx/kind"        # creates local Kubernetes clusters with kind
      version = "~> 0.6"
    }
    helm = {
      source  = "hashicorp/helm"     # installs packaged apps (like KEDA) into the cluster
      version = "~> 2.17"
    }
    kubectl = {
      source  = "alekc/kubectl"      # applies our own YAML files from the k8s folder
      version = "~> 2.1"
    }
  }
}
