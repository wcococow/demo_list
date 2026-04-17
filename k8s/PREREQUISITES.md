# Kubernetes Deployment Prerequisites

Everything you must have in place before running `make deploy`.

---

## 1. Kubernetes Cluster with K3s (`00-namespace.yml`)

K3s must be running on a Linux host **with systemd** (not a container).

```bash
make install-k3s          # installs k3s + writes ~/.kube/config
```

**Why K3s specifically:** the storage manifests use `storageClassName: local-path`, which is K3s's built-in provisioner. On a different cluster (EKS, GKE, etc.) you must change every `storageClassName` to match your provider.

---

## 2. Container Registry + Pushed Image (`04-api.yml`, `05-worker.yml`, `07-flower.yml`)

All three workloads pull `localhost:5000/task-api:latest`. You must build and push the image before deploying.

```bash
make push REGISTRY=myuser          # builds and pushes myuser/task-api:latest
make set-image REGISTRY=myuser     # rewrites the placeholder in all k8s/*.yml
```

The same image is used for `api`, `worker`, and `flower` (different entrypoints via `command:`).

---

## 3. Secrets File (`.env` → `01-secrets.yml`)

`make secrets` reads a `.env` file and creates the `task-api-secrets` Secret. All pods pull credentials from this Secret via `envFrom: secretRef`.

**Required keys:**

| Key | Example value | Used by |
|---|---|---|
| `POSTGRES_DB` | `tasksdb` | postgres, api, worker |
| `POSTGRES_USER` | `tasksuser` | postgres, api, worker |
| `POSTGRES_PASSWORD` | *(strong password)* | postgres, api, worker |
| `DATABASE_URL` | `postgresql://tasksuser:<pw>@postgres:5432/tasksdb` | api, worker, flower |
| `REDIS_PASSWORD` | *(strong password)* | redis, api, worker, flower |
| `REDIS_URL` | `redis://:<pw>@redis:6379/0` | api, worker, flower |
| `SECRET_KEY` | *(random 32+ char string)* | api (JWT signing) |
| `GRAFANA_PASSWORD` | *(admin password)* | grafana |

Create the `.env` file, then run:

```bash
make secrets              # creates namespace + k8s Secret from .env
```

> **Never commit `.env` or `k8s/01-secrets.yml` to git.** The example file `01-secrets.example.yml` shows the shape only.

---

## 4. Persistent Storage — `local-path` provisioner (`02-postgres.yml`, `03-redis.yml`, `09-prometheus.yml`, `10-grafana.yml`, `13-db-backup.yml`)

Five PVCs are created automatically by K3s's `local-path` provisioner. No manual setup needed on K3s.

| PVC | Size | Service |
|---|---|---|
| `postgres-pvc` | 5 Gi | Postgres data |
| `redis-pvc` | 1 Gi | Redis AOF / RDB |
| `prometheus-pvc` | 10 Gi | Metrics (15-day retention) |
| `grafana-pvc` | 2 Gi | Dashboards & alert state |
| `backup-pvc` | 10 Gi | Daily pg_dump backups |

On a non-K3s cluster, replace `local-path` with the appropriate `StorageClass` name (`gp2`, `standard`, etc.).

---

## 5. KEDA — Celery Worker Autoscaler (`06-keda-worker-scaler.yml`)

KEDA is required to scale workers based on Redis queue depth. Install it once before deploying:

```bash
make install-keda         # applies the KEDA v2.14 manifests
```

The `ScaledObject` watches the `celery` list in Redis DB 1 and targets **1 worker per 10 queued jobs**, between 1 and 20 replicas with a 30-second cooldown.

> If you skip KEDA, the `ScaledObject` apply will fail. You can remove `06-keda-worker-scaler.yml` from the `k8s/` directory and rely on the static `replicas: 2` in `05-worker.yml` instead.

---

## 6. Prometheus RBAC — Pod Discovery (`09-prometheus.yml`)

Prometheus uses `kubernetes_sd_configs` to scrape pods annotated with:

```yaml
prometheus.io/scrape: "true"
prometheus.io/port:   "8000"
prometheus.io/path:   "/metrics"
```

These annotations are already set on the `api` pod template in `04-api.yml`. However, Prometheus needs permission to list pods. You may need to create a `ClusterRole` / `ClusterRoleBinding` if your cluster has RBAC enforced (K3s enables RBAC by default):

```bash
kubectl create clusterrolebinding prometheus-reader \
  --clusterrole=view \
  --serviceaccount=task-api:default \
  -n task-api
```

---

## 7. Ingress + Domain (`11-ingress.yml`)

K3s ships Traefik as the ingress controller. You need:

- A **public domain** (or a local `/etc/hosts` entry for testing)
- Replace every `YOUR_DOMAIN` placeholder in `11-ingress.yml` with your real domain

```bash
sed -i 's/YOUR_DOMAIN/yourdomain.com/g' k8s/11-ingress.yml
```

Four subdomains are routed:

| Host | Service | Port |
|---|---|---|
| `yourdomain.com` | api | 8000 |
| `grafana.yourdomain.com` | grafana | 3000 |
| `jaeger.yourdomain.com` | jaeger | 16686 |
| `flower.yourdomain.com` | flower | 5555 |

Without a domain, use `make pf-api` / `make pf-grafana` etc. for local port-forwards.

---

## 8. TLS / cert-manager (`12-cert-manager.yml`) — optional

For HTTPS you need cert-manager installed and a valid email for Let's Encrypt:

```bash
# Install cert-manager once
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml

# Replace placeholder then apply
sed -i 's/YOUR_EMAIL/you@example.com/g' k8s/12-cert-manager.yml
kubectl apply -f k8s/12-cert-manager.yml
```

**Requirements:** port 80 must be publicly reachable for the HTTP-01 ACME challenge.  
Skip this file entirely if you don't need HTTPS.

---

## 9. Daily DB Backup (`13-db-backup.yml`)

Runs `pg_dump | gzip` at 02:00 every day into the `backup-pvc` volume, keeping the last 7 dumps. No extra setup needed — it uses the same `task-api-secrets` as the rest of the stack.

Verify after first run:
```bash
kubectl get jobs -n task-api
kubectl logs job/<db-backup-job-name> -n task-api
```

---

## Checklist

Before `make deploy`, confirm:

- [ ] K3s running and `~/.kube/config` configured
- [ ] Docker image built and pushed; `YOUR_REGISTRY` replaced in manifests
- [ ] `.env` file created with all 8 required keys
- [ ] `make secrets` run (namespace + Secret created)
- [ ] KEDA installed (`make install-keda`) — or `06-keda-worker-scaler.yml` removed
- [ ] `YOUR_DOMAIN` replaced in `11-ingress.yml`
- [ ] *(optional)* `YOUR_EMAIL` replaced in `12-cert-manager.yml` and cert-manager installed
- [ ] *(optional)* Prometheus RBAC ClusterRoleBinding created
