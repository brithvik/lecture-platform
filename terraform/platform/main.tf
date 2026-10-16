# Layer 2: everything that runs on the cluster (KEDA and the app).
# It reads the cluster's address and keys from the cluster layer's results, so the cluster
# always exists before this plans: no chicken-and-egg, no -target needed.
data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = "${path.module}/../cluster/terraform.tfstate"
  }
}

locals {
  cluster = data.terraform_remote_state.cluster.outputs
}

provider "helm" {
  kubernetes {
    host                   = local.cluster.endpoint
    client_certificate     = local.cluster.client_certificate
    client_key             = local.cluster.client_key
    cluster_ca_certificate = local.cluster.cluster_ca_certificate
  }
}

provider "kubectl" {
  host                   = local.cluster.endpoint
  client_certificate     = local.cluster.client_certificate
  client_key             = local.cluster.client_key
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
  load_config_file       = false
}

# kind can't pull images from your laptop on its own, so copy the app image in.
# (Local only: a cloud cluster pulls from a registry instead.)
resource "terraform_data" "load_image" {
  triggers_replace = [var.app_image, local.cluster.endpoint]

  provisioner "local-exec" {
    command = "kind load docker-image ${var.app_image} --name ${local.cluster.cluster_name}"
  }
}

resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_version
  namespace        = "keda"
  create_namespace = true
  wait             = true
}

data "kubectl_path_documents" "app" {
  pattern = "${path.module}/../../k8s/*.yaml"
}

resource "kubectl_manifest" "app" {
  for_each   = data.kubectl_path_documents.app.manifests
  yaml_body  = each.value
  depends_on = [helm_release.keda, terraform_data.load_image]
}
