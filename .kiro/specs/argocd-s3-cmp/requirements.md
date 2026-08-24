# Requirements: ArgoCD S3 Helm Charts via Sidecar CMP

## Introduction

ArgoCD cannot natively fetch Helm charts from an `s3://` URL because its repo-server has no `helm-s3` plugin and does not perform AWS SigV4 signing — it fails with `unsupported protocol scheme "s3"`. The Todo App's CD currently points at `s3://donee-s3/charts` and therefore cannot reconcile from a live pull.

This feature enables ArgoCD to pull the Todo App Helm chart directly from the private S3 bucket by adding a **sidecar Config Management Plugin (CMP)** to the `argocd-repo-server`. The sidecar contains Helm plus the `helm-s3` plugin and generates rendered manifests on ArgoCD's behalf. Authentication to the private bucket uses IRSA (an IAM role bound to the repo-server service account). The S3 bucket stays private — no public-read, no CloudFront, no migration to ECR/OCI.

The chart-publishing pipeline (GitHub Actions + `helm s3 push`, auto-versioned `MAJOR.MINOR.<run_number>`) and the ArgoCD SemVer range (`>=0.2.0 <1.0.0`) established earlier are retained. Because a CMP replaces ArgoCD's native Helm source, the plugin script is responsible for resolving the SemVer range to the newest matching chart version.

### Environment (confirmed from existing configs)
- AWS account: `014032199917`
- Region: `us-east-1`
- Helm S3 repo: `s3://donee-s3/charts`
- Chart name: `todo-app`, deployed to namespace `donee`
- ArgoCD namespace: `argocd`; ArgoCD `repo-server` uses a ClusterIP service (no external exposure)

### Non-goals
- Migrating the chart to ECR/OCI.
- Making the S3 bucket or `charts/` prefix public.
- Changing the CI publishing mechanism or the versioning scheme.
- Moving the Docker image off Docker Hub.

---

## Requirements

### Requirement 1: ArgoCD renders the chart from a private S3 bucket
**User Story:** As a platform operator, I want ArgoCD to pull and render the Todo App chart from `s3://donee-s3/charts` so that GitOps reconciliation works without exposing the bucket or leaving S3.

#### Acceptance Criteria
1. WHEN the ArgoCD Application for `todo-app` is refreshed THEN ArgoCD SHALL generate manifests from the chart stored in `s3://donee-s3/charts` without the `unsupported protocol scheme "s3"` error.
2. WHEN ArgoCD generates the manifests THEN the rendering SHALL be performed by the sidecar CMP (containing Helm + `helm-s3`), not by ArgoCD's native Helm source.
3. THE S3 bucket `donee-s3` SHALL remain private throughout (no public-read bucket/prefix policy, no CloudFront distribution introduced).
4. THE existing `helm s3 push` CI publishing flow and the `MAJOR.MINOR.<run_number>` versioning SHALL continue to function unchanged.

### Requirement 2: Sidecar CMP on the repo-server (Variant B, not a rebuilt main image)
**User Story:** As a platform operator, I want the helm-s3 capability added as a sidecar to the repo-server so that I avoid maintaining a custom `argocd-repo-server` base image.

#### Acceptance Criteria
1. THE solution SHALL add a sidecar container to the `argocd-repo-server` Deployment rather than replacing the main repo-server image.
2. THE sidecar SHALL contain Helm and the `helm-s3` plugin (plus AWS SDK/CLI capability required for SigV4 signing).
3. THE sidecar SHALL be registered as a ConfigManagementPlugin via a `plugin.yaml` mounted into the sidecar (sidecar-CMP discovery), consistent with the currently recommended CMP pattern (not the deprecated `argocd-cm` `configManagementPlugins` field).
4. THE repo-server patch SHALL provide the shared volumes required by CMP sidecars (e.g. the CMP server socket volume and a plugin/tmp working volume) so the sidecar and repo-server can exchange generated manifests.
5. WHEN the CMP sidecar is added THEN existing native (git/Helm/OCI) ArgoCD Applications SHALL continue to reconcile unaffected.

### Requirement 3: Private-bucket authentication via IRSA
**User Story:** As a security-conscious operator, I want the repo-server to authenticate to S3 with a scoped IAM role so that no static credentials are stored and access is least-privilege.

#### Acceptance Criteria
1. THE repo-server (and its CMP sidecar) SHALL authenticate to S3 using an IAM role assumed via IRSA bound to the repo-server's Kubernetes ServiceAccount.
2. THE IAM policy SHALL grant read-only access scoped to the chart location only: `s3:GetObject` and `s3:ListBucket` on `arn:aws:s3:::donee-s3` restricted to the `charts/*` prefix (and `s3:GetBucketLocation` if required by the plugin).
3. THE IAM policy SHALL NOT grant write/delete on the bucket, and SHALL NOT grant access to other buckets or prefixes.
4. THE IAM role trust policy SHALL restrict assumption to the specific EKS OIDC provider AND the specific `system:serviceaccount:argocd:<repo-server-sa>` subject.
5. IF the assumed role lacks permission or IRSA is misconfigured THEN the plugin SHALL fail manifest generation with a clear error surfaced in the ArgoCD Application status (not a silent success or an empty render).

### Requirement 4: SemVer range resolution inside the plugin
**User Story:** As a developer, I want a `Chart.yaml` MAJOR.MINOR bump to still auto-deploy the newest matching version so that the SemVer-range behavior is preserved even though a CMP now does the rendering.

#### Acceptance Criteria
1. THE ArgoCD Application SHALL express the desired version as a SemVer range (`>=0.2.0 <1.0.0`), carried in a way the plugin can read (e.g. a plugin parameter / env var), rather than a pinned version.
2. WHEN the plugin generates manifests THEN it SHALL query the S3 Helm repo index, select the HIGHEST chart version satisfying the configured SemVer range, and render that version.
3. WHEN a new chart version is published to S3 that satisfies the range AND is higher than the currently deployed version THEN a subsequent ArgoCD refresh/poll SHALL result in the newer version being rendered and synced.
4. WHEN a published version falls OUTSIDE the range (e.g. `1.0.x` against `>=0.2.0 <1.0.0`) THEN the plugin SHALL NOT select it.
5. IF no version in the repo satisfies the range THEN the plugin SHALL fail generation with a clear error rather than rendering nothing.
6. THE plugin SHALL refresh its view of the S3 repo index on each generation (or invalidate cache appropriately) so that newly pushed versions are discoverable without manual repo-server restarts.

### Requirement 5: Application wired to the plugin with existing value overrides
**User Story:** As an operator, I want the `todo-app` Application to use the CMP while keeping the current Helm value overrides so that behavior (existing MySQL secret, image repo, storage class) is unchanged.

#### Acceptance Criteria
1. THE `ArgoCD/todo-app.yaml` SHALL be updated to use `spec.source.plugin` (referencing the CMP) instead of `spec.source.helm`.
2. THE plugin invocation SHALL pass through the existing Helm values currently in the Application: `replicaCount`, `image.repository`, `mysql.auth.existingSecret: todo-app-mysql-secret`, and `mysql.storage.storageClass: gp2`.
3. THE rendered output SHALL continue to reference the pre-existing `todo-app-mysql-secret` and SHALL NOT embed plaintext DB credentials.
4. THE Application `destination` (namespace `donee`) and `syncPolicy` (automated, prune, selfHeal) SHALL be preserved.

### Requirement 6: Verifiable rollout and rollback
**User Story:** As an operator, I want to confirm the new version actually deploys and to be able to roll back so that changes are safe and observable.

#### Acceptance Criteria
1. AFTER the CMP and IRSA are in place and the Application is applied, WHEN ArgoCD refreshes THEN `argocd app get todo-app` SHALL show a successful sync to the newest in-range version (e.g. `0.2.<run_number>`) with Health `Healthy` and no `ComparisonError`.
2. WHEN the new revision rolls out THEN the `todo-app` Deployment pods in `donee` SHALL become `Ready` with the readiness probe (`/readyz`) passing.
3. THE change SHALL be reversible: reverting the Application (or narrowing the range / re-applying the prior spec) SHALL return ArgoCD to the previous behavior.
4. THE feature SHALL NOT cause an outage of the currently-running app during rollout (rolling update; old pods remain until new pods are Ready).

### Requirement 7: Artifacts producible in-repo; cluster/AWS steps documented
**User Story:** As the implementer, I want all cluster-applyable artifacts and AWS steps captured in the repo so that the operator can apply them without guesswork.

#### Acceptance Criteria
1. THE repo SHALL contain: the CMP `plugin.yaml`, the `argocd-repo-server` sidecar/volume patch, the sidecar container image definition (Dockerfile or a documented public image reference), the IRSA IAM policy JSON and trust policy JSON, and the updated `ArgoCD/todo-app.yaml`.
2. THE spec SHALL clearly delineate which steps are applied in-cluster / in-AWS by the operator (building/pushing the sidecar image, creating the IAM role, associating IRSA, patching the ArgoCD install) versus which are file changes in this repo.
3. WHERE a step cannot be executed from this workspace (image build/push, IAM creation, cluster patching), THE spec SHALL provide the exact commands for the operator to run.
