# Todo App

A Kanban-style Todo application built with Flask and MySQL, featuring a modern, multi-page responsive UI with a command palette, task editing, drag-and-drop scheduling, analytics, dark mode, and per-user shareable lists. Fully containerized with Docker and deployable via Kubernetes using Helm charts and an ArgoCD GitOps pipeline on AWS EKS.

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Local Development with Docker Compose](#local-development-with-docker-compose)
  - [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [Deployment](#deployment)
  - [Docker](#docker)
  - [Kubernetes (Raw Manifests)](#kubernetes-raw-manifests)
  - [Helm Chart](#helm-chart)
  - [ArgoCD GitOps](#argocd-gitops)
- [CI/CD Pipeline](#cicd-pipeline)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

## Features

**Task management**

- Three-bucket Kanban board: **Today**, **This Week**, **Eventually**, with an automatic **Overdue** column
- Drag-and-drop between buckets (due date is the source of truth for bucketing)
- Add, complete, delete, and move tasks; insert at a specific position; bulk-delete completed
- **Task edit modal** — click a card to edit its title, notes, due date, and bucket
- **Due dates** with overdue / due-today awareness

**Productivity UX**

- **Command palette (⌘K / Ctrl+K)** — jump between pages, quick-add a task, or search open tasks
- **Confetti** micro-interaction on completion (respects `prefers-reduced-motion`)
- **Toasts with Undo** for completing and deleting tasks
- In-app **notifications** for overdue and due-today tasks (dismissible, persisted per list)

**Pages**

- **Dashboard** — greeting, bucket stat cards, week strip, momentum rings, and an activity streak
- **Overview** — completion-rate hero ring, key metrics, category breakdown, and recent activity
- **Analytics** — completion ring, category bars, weekly completion sparkline, and a consistency heatmap
- **Schedule** — a full calendar (FullCalendar) with drag-to-reschedule and click-to-add
- **Completed** — finished tasks grouped by bucket with a one-click **Restore**

**Accounts & platform**

- Email/password **authentication** with signup, login, logout, and password reset
- Unique **shareable list URLs** (each account gets its own list ID; ownership enforced)
- Security hardening: CSRF protection, rate limiting, secure session cookies
- Dark mode and accent color themes; fully responsive (mobile-friendly)
- MySQL persistent storage with startup retry logic and an idempotent `due_date` migration
- Health probes (`/healthz`, `/readyz`) for Kubernetes

## Tech Stack

| Layer          | Technology                                    |
| -------------- | --------------------------------------------- |
| Backend        | Python 3.11, Flask 3.0.3, Flask-SQLAlchemy    |
| Database       | MySQL 8.0 (PyMySQL driver)                    |
| Frontend       | Bootstrap 5.3, Vanilla JavaScript             |
| Containerization | Docker, Docker Compose                      |
| Orchestration  | Kubernetes (EKS), Helm 3                      |
| GitOps         | ArgoCD with Helm S3 repository                |
| CI/CD          | GitHub Actions (OIDC auth to AWS)             |
| Cloud          | AWS (EKS, ALB, ACM, S3, IAM Pod Identity)    |

## Architecture

```
Developer Push (main)
        |
        v
GitHub Actions CI
  ├── Build multi-arch Docker image --> Docker Hub (doni502/todos)
  └── Package Helm chart --> S3 (s3://donee-s3/charts)
        |
        v
ArgoCD (on EKS, Pod Identity for S3 access)
  └── Auto-syncs Helm chart to EKS namespace "donee"
        |
        v
AWS ALB Ingress (HTTPS via ACM certificate)
  └── Exposes the app to the internet
```

## Getting Started

### Prerequisites

- Docker and Docker Compose
- (Optional) An [Unsplash API](https://unsplash.com/developers) access key for dynamic backgrounds

### Local Development with Docker Compose

1. Clone the repository:

```bash
git clone https://github.com/Doni1905/TODOS.git
cd TODOS
```

2. (Optional) Create a `.env` file with your secrets:

```bash
MYSQL_USER=your_db_user
MYSQL_PASSWORD=your_db_password
MYSQL_DATABASE=your_db_name
UNSPLASH_ACCESS_KEY=your_unsplash_access_key
```

3. Start the application:

```bash
docker compose up --build
```

4. Open your browser at `http://localhost:5001`

The app will automatically create the database tables on first run. MySQL data is persisted in a named Docker volume.

### Environment Variables

| Variable              | Description                         | Required |
| --------------------- | ----------------------------------- | -------- |
| `MYSQL_HOST`          | MySQL hostname                      | Yes      |
| `MYSQL_PORT`          | MySQL port                          | Yes      |
| `MYSQL_USER`          | MySQL username                      | Yes      |
| `MYSQL_PASSWORD`      | MySQL password                      | Yes      |
| `MYSQL_DATABASE`      | MySQL database name                 | Yes      |
| `UNSPLASH_ACCESS_KEY` | Unsplash API key for backgrounds    | No       |

## API Reference

Data endpoints are scoped to a list ID (`/api/<list_id>/...`) and require an authenticated session that owns the list.

| Method   | Endpoint                            | Description                              |
| -------- | ----------------------------------- | ---------------------------------------- |
| `GET`    | `/api/<list_id>/todos`              | Get all todos for a list (ordered by position) |
| `POST`   | `/api/<list_id>/todo`               | Create a new todo (optional `due_date`)  |
| `POST`   | `/api/<list_id>/todo/insert/index`  | Insert a todo at a specific index        |
| `PUT`    | `/api/<list_id>/todo/<id>`          | Update a todo (title, description, completed, category, due_date) |
| `PATCH`  | `/api/<list_id>/todo/<id>/move`     | Move a todo to a different category      |
| `PATCH`  | `/api/<list_id>/todo/<id>/date`     | Set or clear a todo's due date (calendar reschedule) |
| `DELETE` | `/api/<list_id>/todo/<id>`          | Delete a todo                            |
| `DELETE` | `/api/<list_id>/todos/completed`    | Delete all completed todos in a list     |
| `GET`    | `/api/<list_id>/stats`              | Completion stats for the list            |
| `GET`    | `/api/<list_id>/calendar`           | Dated tasks as calendar events           |

**Pages (session-authenticated):** `/login`, `/signup`, `/logout`, `/forgot-password`, `/reset-password/<token>`, `/list/<list_id>` (board), `/dashboard`, `/overview`, `/analytics`, `/schedule`, `/completed`

**Health probes (no auth):** `GET /healthz` (liveness), `GET /readyz` (readiness, checks the DB)

### Request/Response Examples

**Create a todo:**

```bash
curl -X POST http://localhost:5001/api/abc12345/todo \
  -H "Content-Type: application/json" \
  -d '{"title": "Buy groceries", "category": "today", "description": "Milk, eggs, bread"}'
```

**Response (201):**

```json
{
  "id": 1,
  "list_id": "abc12345",
  "title": "Buy groceries",
  "description": "Milk, eggs, bread",
  "completed": false,
  "category": "today",
  "position": 0,
  "due_date": null
}
```

**Valid categories:** `today`, `this_week`, `eventually`

## Deployment

### Docker

Build and run the image standalone:

```bash
docker build -t todo-app .
docker run -p 5001:5001 \
  -e MYSQL_HOST=your-mysql-host \
  -e MYSQL_USER=your-db-user \
  -e MYSQL_PASSWORD=your-db-password \
  -e MYSQL_DATABASE=your-db-name \
  todo-app
```

### Kubernetes (Raw Manifests)

Deploy to a Kubernetes cluster using the manifests in `k8s/`:

```bash
# Create the secret first (copy and edit the example)
cp k8s/mysql-secret.example.yaml k8s/mysql-secret.yaml
# Edit k8s/mysql-secret.yaml with your base64-encoded credentials

kubectl apply -f k8s/mysql-secret.yaml
kubectl apply -f k8s/mysql-statefulset-pvc.yaml
kubectl apply -f k8s/flask-deployment.yaml
kubectl apply -f k8s/flask-service.yaml
kubectl apply -f k8s/flask-ingress.yaml
```

### Helm Chart

Install using the Helm chart in `Todo_app/`:

```bash
# Install with default values
helm install todo-app ./Todo_app

# Install with custom values
helm install todo-app ./Todo_app \
  --set mysql.auth.password=<your-password> \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host=todo.example.com

# Upgrade an existing release
helm upgrade todo-app ./Todo_app --set image.tag=v2.0
```

Key Helm values:

| Value                  | Description                | Default            |
| ---------------------- | -------------------------- | ------------------ |
| `replicaCount`         | Flask app replicas         | `1`                |
| `image.repository`     | Docker image               | `doni502/todos`    |
| `image.tag`            | Image tag                  | `latest`           |
| `mysql.enabled`        | Deploy MySQL               | `true`             |
| `mysql.auth.password`  | MySQL password             | (set in secret)    |
| `mysql.storage.size`   | PVC size for MySQL         | `1Gi`              |
| `ingress.enabled`      | Enable Ingress             | `false`            |
| `flask.env.UNSPLASH_ACCESS_KEY` | Unsplash API key | (empty)            |

### ArgoCD GitOps

The project includes a full GitOps setup with ArgoCD pulling Helm charts from an S3 bucket.

**Components:**

- `ArgoCD/base/` - Base Kustomize manifests
- `ArgoCD/overlays/production/` - Production patches (2 replicas, resource limits, rolling updates)
- `pod-identity-setup/` - AWS IAM roles, trust policies, and Pod Identity configuration

**Production overlay applies:**

- 2 replicas with rolling update strategy
- Resource requests: 100m CPU / 128Mi RAM
- Resource limits: 500m CPU / 256Mi RAM
- HTTPS via AWS ACM certificate on ALB

To set up the full GitOps pipeline, run the setup script:

```bash
chmod +x pod-identity-setup/07-apply-all.sh
./pod-identity-setup/07-apply-all.sh
```

## CI/CD Pipeline

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs on pushes to `main`/`master`:

1. **Build** - Multi-platform Docker image (amd64 + arm64) with layer caching
2. **Push** - Tags image with commit SHA and `latest`, pushes to Docker Hub
3. **Auth** - Assumes AWS role via OIDC federation (no stored credentials)
4. **Package** - Updates Helm chart version and image tag, packages chart
5. **Deploy** - Pushes packaged chart to S3 Helm repository

ArgoCD detects the new chart version and auto-syncs the deployment.

## Project Structure

```
.
├── app_db.py                    # Flask application (routes, models, config)
├── templates/                   # Jinja templates (one per page + shared partials)
│   ├── _layout.html             # Base layout (topbar, notifications, shared JS)
│   ├── _fluid_menu.html         # Floating navigation menu
│   ├── _confetti.html           # Shared confetti micro-interaction
│   ├── _auth_styles.html        # Shared styles for auth pages
│   ├── index.html               # Kanban board (My Tasks) + command palette + task modal
│   ├── dashboard.html           # Dashboard (stats, week strip, momentum, streak)
│   ├── overview.html            # Overview (completion ring, metrics, activity)
│   ├── analytics.html           # Analytics (sparkline + consistency heatmap)
│   ├── schedule.html            # Calendar (FullCalendar)
│   ├── completed.html           # Completed tasks (grouped, with Restore)
│   ├── login.html / signup.html # Authentication
│   └── forgot_password.html / reset_password.html
├── Dockerfile                   # Container image definition
├── docker-compose.yml           # Local dev environment
├── requirements.txt             # Python dependencies
├── .github/workflows/
│   └── ci.yml                   # CI/CD pipeline
├── k8s/                         # Raw Kubernetes manifests
│   ├── flask-deployment.yaml
│   ├── flask-service.yaml
│   ├── flask-ingress.yaml
│   ├── mysql-statefulset-pvc.yaml
│   └── mysql-secret.example.yaml
├── Todo_app/                    # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
├── ArgoCD/                      # Kustomize overlays for ArgoCD
│   ├── base/
│   └── overlays/production/
└── pod-identity-setup/          # AWS IAM & Pod Identity scripts
    ├── 01-github-trust-policy.json
    ├── 02-github-s3-policy.json
    ├── 03-pod-identity-trust-policy.json
    ├── 04-argocd-s3-policy.json
    ├── 05-argocd-repo-secret.yaml
    ├── 06-argocd-application.yaml
    └── 07-apply-all.sh
```

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Add my feature"`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request

## License

This project is open source.
