# ---------- 1. The cluster ----------
resource "kind_cluster" "this" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }
  }
}

# Connect the other providers to the cluster Terraform just created
provider "helm" {
  kubernetes {
    host                   = kind_cluster.this.endpoint
    client_certificate     = kind_cluster.this.client_certificate
    client_key             = kind_cluster.this.client_key
    cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  }
}

provider "kubectl" {
  host                   = kind_cluster.this.endpoint
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  load_config_file       = false
}

# ---------- 2. The app image ----------
# kind can't pull images from your laptop on its own, so copy it in.
# Re-runs whenever the image tag or the cluster changes.
resource "terraform_data" "load_image" {
  triggers_replace = [var.app_image, kind_cluster.this.id]

  provisioner "local-exec" {
    command = "kind load docker-image ${var.app_image} --name ${var.cluster_name}"
  }
}

# ---------- 3. KEDA (autoscaling) ----------
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_version
  namespace        = "keda"
  create_namespace = true
  wait             = true   # don't continue until KEDA is running
}

# ---------- 4. The app: everything in the k8s folder ----------
data "kubectl_path_documents" "app" {
  pattern = "${path.module}/../k8s/*.yaml"
}

resource "kubectl_manifest" "app" {
  for_each  = data.kubectl_path_documents.app.manifests
  yaml_body = each.value

  # keda.yaml needs KEDA installed first, and the pods need the image loaded
  depends_on = [helm_release.keda, terraform_data.load_image]
}
