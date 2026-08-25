# Serving the Helm chart to Argo CD over HTTPS (S3 static repo)

`ArgoCD/todo-app.yaml` points at:

    repoURL: https://donee-s3.s3.us-east-1.amazonaws.com/charts

Argo CD's `argocd-repo-server` reads this with its **built-in HTTP getter** —
it fetches `charts/index.yaml`, reads each chart's download URL from the index,
then GETs the `.tgz`. No `helm-s3` plugin, sidecar CMP, or custom repo-server
image is required on the consume side.

> The `helm-s3` plugin is still used by the **release pipeline** to *push*
> charts to S3 (that side uses AWS credentials via OIDC). Only the *consume*
> side moved to HTTPS.

## Requirements for the HTTPS repo to work

1. **The `charts/` prefix must be readable over anonymous HTTPS.** Helm's HTTP
   repo client does not sign S3 requests (no SigV4), so private objects return
   `403`. Make the prefix public via a bucket policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "PublicReadCharts",
         "Effect": "Allow",
         "Principal": "*",
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::donee-s3/charts/*"
       }
     ]
   }
   ```

2. **`index.yaml` must list HTTPS download URLs**, not `s3://`. The `helm s3
   push` plugin writes `s3://` URLs into the index, which the HTTP getter cannot
   fetch. The release workflow regenerates the index with HTTPS URLs after every
   push (`helm repo index --url https://donee-s3.s3.us-east-1.amazonaws.com/charts`).

   To rebuild the index manually:

   ```bash
   mkdir -p /tmp/index-build
   aws s3 sync s3://donee-s3/charts/ /tmp/index-build/ --exclude "*" --include "*.tgz"
   helm repo index /tmp/index-build --url https://donee-s3.s3.us-east-1.amazonaws.com/charts
   aws s3 cp /tmp/index-build/index.yaml s3://donee-s3/charts/index.yaml --content-type "text/yaml"
   ```

## Register the repo in Argo CD

`repo-secret.yaml` registers the HTTPS endpoint as a Helm-type repository:

    kubectl apply -f repo-secret.yaml

## Verify

    # Index and chart tarball must both return HTTP 200 over HTTPS
    curl -s -o /dev/null -w "index: %{http_code}\n" \
      https://donee-s3.s3.us-east-1.amazonaws.com/charts/index.yaml
    curl -s -o /dev/null -w "chart: %{http_code}\n" \
      https://donee-s3.s3.us-east-1.amazonaws.com/charts/todo-app-2.0.3.tgz

    # The index must list HTTPS urls, not s3://
    curl -s https://donee-s3.s3.us-east-1.amazonaws.com/charts/index.yaml | grep -A1 "urls:"

Then hit **Refresh** on the `todo-app` Application in the UI. If you get a `403`,
revisit the bucket policy (step 1). If the index loads but the chart pull fails,
the index still has `s3://` URLs — rebuild it (step 2).
