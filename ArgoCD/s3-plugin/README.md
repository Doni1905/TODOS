# Enabling `s3://` Helm charts in Argo CD

`ArgoCD/todo-app.yaml` points at `repoURL: s3://donee-s3/charts`. Argo CD's
`argocd-repo-server` cannot resolve `s3://` on its own, which is why you see:

    unsupported protocol scheme "s3"

The repo-server needs the [`helm-s3`](https://github.com/hypnoglow/helm-s3)
plugin (latest release: v0.17.2). Pick the install method that matches how you
deployed Argo CD.

## 1. Install the plugin into the repo-server

### If Argo CD was installed via the official Helm chart (`argo/argo-cd`)

    helm repo add argo https://argoproj.github.io/argo-helm
    helm upgrade --install argocd argo/argo-cd \
      --namespace argocd \
      -f values-argocd-helm.yaml

### If Argo CD was installed via raw manifests (`kubectl apply`)

    kubectl patch deployment argocd-repo-server -n argocd \
      --patch-file repo-server-patch.yaml
    kubectl rollout status deployment/argocd-repo-server -n argocd

## 2. Register the S3 bucket as a Helm repository

Argo CD still needs to know `donee-s3` is a Helm-type repo. Create a repo
secret in the `argocd` namespace:

    apiVersion: v1
    kind: Secret
    metadata:
      name: donee-s3-charts
      namespace: argocd
      labels:
        argocd.argoproj.io/secret-type: repository
    stringData:
      name: donee-s3-charts
      type: helm
      url: s3://donee-s3/charts
      enableOCI: "false"

## 3. Give the repo-server AWS credentials

The plugin needs permission to read the bucket. Preferred: IRSA (attach an IAM
role to the `argocd-repo-server` service account) with at least:

    s3:GetObject, s3:ListBucket  on  arn:aws:s3:::donee-s3 and arn:aws:s3:::donee-s3/*

Also make sure the region is set, e.g. add to the repo-server env:

    - name: AWS_REGION
      value: <your-bucket-region>

## 4. Verify

    # Confirm the init container installed the plugin cleanly
    kubectl logs deployment/argocd-repo-server -n argocd -c install-helm-s3-plugin

Then hit **Refresh** on the `todo-app` Application in the UI. The
`unsupported protocol scheme "s3"` error should clear. If you now see an AWS
credential/permission error instead, revisit step 3 (IAM/IRSA + region).
