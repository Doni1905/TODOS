# S3 HTTP Approach for ArgoCD Helm Charts

Instead of using the `s3://` protocol (which requires the `helm-s3` plugin in
the repo-server), you can serve charts over plain HTTPS using the S3 HTTP
endpoint. ArgoCD handles standard HTTP Helm repos natively — no plugin needed.

## How It Works

S3 exposes every object via HTTPS at:

    https://<bucket>.s3.<region>.amazonaws.com/<key>

If your Helm chart package (`todo-app-0.1.2.tgz`) and `index.yaml` live under
the `charts/` prefix in the `donee-s3` bucket, the repo URL becomes:

    https://donee-s3.s3.us-east-1.amazonaws.com/charts

ArgoCD fetches `index.yaml` from that URL, resolves the chart version, and
downloads the `.tgz` — all over HTTPS.

## Prerequisites

1. **Bucket must be accessible** from the cluster (either publicly or via a VPC
   endpoint). If using a VPC endpoint, attach a bucket policy allowing
   `s3:GetObject` from the VPCE (see `s3-vpce-getobject-policy.json`).

2. **`index.yaml` must exist** at the charts prefix. Generate it with:

       helm repo index ./charts --url https://donee-s3.s3.us-east-1.amazonaws.com/charts
       aws s3 cp ./charts/index.yaml s3://donee-s3/charts/index.yaml

3. **Chart `.tgz` must be uploaded** alongside the index:

       aws s3 cp todo-app-0.1.2.tgz s3://donee-s3/charts/todo-app-0.1.2.tgz

## Deployment

### 1. Register the repo in ArgoCD

    kubectl apply -f repo-secret-http.yaml

This creates a Secret that tells ArgoCD the HTTPS URL is a Helm repository.

### 2. Deploy the Application

    kubectl apply -f ../todo-app-s3-http.yaml

### 3. Verify

    argocd app get todo-app

The app should sync without any `unsupported protocol scheme "s3"` error.

## When to Use This vs. the `s3://` Plugin

| Approach | Pros | Cons |
|----------|------|------|
| **S3 HTTP** | No plugin needed, simpler repo-server, works with any S3-compatible store | Bucket must allow HTTP reads (public or VPCE), requires `index.yaml` maintenance |
| **helm-s3 plugin** | Native S3 auth via IAM/IRSA, no bucket policy changes | Adds init container, plugin version management, harder to debug |

Choose **S3 HTTP** when your cluster can reach the bucket over HTTPS (e.g. via
a VPC endpoint) and you want a zero-plugin setup.

## File Reference

- `repo-secret-http.yaml` — Repo Secret for the HTTPS endpoint
- `../todo-app-s3-http.yaml` — ArgoCD Application using HTTPS repoURL
- `../../s3-vpce-getobject-policy.json` — Example VPC endpoint bucket policy
