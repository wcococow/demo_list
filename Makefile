REGISTRY   ?= yourname
IMAGE      ?= task-api
TAG        ?= latest
NAMESPACE  ?= task-api
FULL_IMAGE  = $(REGISTRY)/$(IMAGE):$(TAG)
LOCAL_REGISTRY = localhost:5000
LOCAL_IMAGE    = $(LOCAL_REGISTRY)/$(IMAGE):$(TAG)

# ── all-in-one: registry → build → push → patch manifests → deploy ───────────

all: local-registry build-local push-local patch-local deploy restart wait-ready
	@echo ""
	@echo "Stack is up. Run 'make status' to check pods."
	@echo "Port-forward shortcuts: make pf-api | pf-grafana | pf-jaeger | pf-flower"

# Force-restart all deployments (clears stale containerd state)
restart:
	kubectl rollout restart deployment -n $(NAMESPACE)

# Wait until all pods are Running
wait-ready:
	@echo "Waiting for all pods to be Ready…"
	@timeout 180 sh -c 'until kubectl get pods -n $(NAMESPACE) 2>/dev/null | grep -v "Running\|Completed\|NAME" | grep -qv "^$$"; do true; done; true'
	kubectl get pods -n $(NAMESPACE)

# Start a local Docker registry and restart k3s to pick up the registry config
local-registry:
	@docker inspect registry 2>/dev/null | grep -q '"Running": true' \
	  && echo "Local registry already running" \
	  || docker run -d -p 5000:5000 --restart=always --name registry registry:2
	@sudo mkdir -p /etc/rancher/k3s
	@printf 'mirrors:\n  "localhost:5000":\n    endpoint:\n      - "http://localhost:5000"\n' \
	  | sudo tee /etc/rancher/k3s/registries.yaml > /dev/null
	@echo "Restarting k3s to pick up registry config…"
	@sudo k3s-killall.sh 2>/dev/null; sleep 2
	@sudo nohup k3s server --disable traefik --snapshotter=native &>/tmp/k3s.log &
	@timeout 120 sh -c 'until kubectl get nodes 2>/dev/null | grep -q Ready; do sleep 3; done'
	@echo "k3s ready."

# Build image and tag for local registry
build-local:
	docker build -t $(LOCAL_IMAGE) ./backend

# Push to local registry (k3s pulls from there)
push-local:
	docker push $(LOCAL_IMAGE)

# Patch manifests to reference local registry image (covers api, worker, flower)
patch-local:
	grep -rl 'YOUR_REGISTRY/task-api:latest' k8s/ | xargs sed -i 's|YOUR_REGISTRY/task-api:latest|$(LOCAL_IMAGE)|g'

# ── local dev ─────────────────────────────────────────────────────────────────

up:
	docker compose up --build

down:
	docker compose down

down-volumes:
	docker compose down -v

test:
	bash backend/test_endpoints.sh

# ── image ─────────────────────────────────────────────────────────────────────

build:
	docker build -t $(FULL_IMAGE) ./backend

push: build
	docker push $(FULL_IMAGE)

# Update image tag in all k8s manifests
set-image:
	sed -i 's|YOUR_REGISTRY/task-api:latest|$(FULL_IMAGE)|g' k8s/*.yml

# ── k3s install ───────────────────────────────────────────────────────────────

install-k3s:
	# Run k3s in background (no systemd required — container-safe)
	curl -sfL https://get.k3s.io | INSTALL_K3S_SKIP_ENABLE=true sh -
	sudo nohup k3s server --disable traefik --snapshotter=native &>/tmp/k3s.log &
	@echo "Waiting for k3s to write kubeconfig…"
	@timeout 60 sh -c 'until [ -f /etc/rancher/k3s/k3s.yaml ]; do sleep 2; done'
	mkdir -p ~/.kube
	sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
	sudo chown $(shell id -u):$(shell id -g) ~/.kube/config
	@echo "Waiting for node to be Ready…"
	@timeout 120 sh -c 'until kubectl get nodes 2>/dev/null | grep -q Ready; do sleep 3; done'
	kubectl get nodes

install-keda:
	kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.14.0/keda-2.14.0.yaml

# ── deploy ────────────────────────────────────────────────────────────────────

# Create secrets from your .env file — run once before deploy
secrets:
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl create secret generic task-api-secrets \
		--from-env-file=.env \
		--namespace=$(NAMESPACE) \
		--dry-run=client -o yaml | kubectl apply -f -

deploy: secrets
	# Skip prod-only manifests: KEDA (needs CRDs), ingress/cert-manager (need a real domain)
	kubectl apply --namespace=$(NAMESPACE) $(shell ls k8s/*.yml \
	  | grep -v 06-keda \
	  | grep -v 11-ingress \
	  | grep -v 12-cert-manager \
	  | sed 's/^/-f /' | tr '\n' ' ')

delete:
	kubectl delete --namespace=$(NAMESPACE) --ignore-not-found $(shell ls k8s/*.yml \
	  | grep -v 06-keda \
	  | grep -v 11-ingress \
	  | grep -v 12-cert-manager \
	  | sed 's/^/-f /' | tr '\n' ' ')

# ── status ────────────────────────────────────────────────────────────────────

status:
	kubectl get pods,svc,hpa,scaledobjects -n $(NAMESPACE)

watch:
	watch kubectl get pods -n $(NAMESPACE)

# ── logs ──────────────────────────────────────────────────────────────────────

logs-api:
	kubectl logs -f -l app=api -n $(NAMESPACE) --all-containers

logs-worker:
	kubectl logs -f -l app=worker -n $(NAMESPACE) --all-containers

logs-db:
	kubectl logs -f -l app=postgres -n $(NAMESPACE)

# ── scaling ───────────────────────────────────────────────────────────────────

scale-worker:
	kubectl scale deployment worker --replicas=$(n) -n $(NAMESPACE)

scale-api:
	kubectl scale deployment api --replicas=$(n) -n $(NAMESPACE)

# ── port-forward (local access without ingress) ───────────────────────────────

pf-api:
	kubectl port-forward svc/api 8000:8000 -n $(NAMESPACE)

pf-grafana:
	kubectl port-forward svc/grafana 3001:3000 -n $(NAMESPACE)

pf-jaeger:
	kubectl port-forward svc/jaeger 16686:16686 -n $(NAMESPACE)

pf-flower:
	kubectl port-forward svc/flower 5555:5555 -n $(NAMESPACE)

.PHONY: all local-registry build-local push-local patch-local restart wait-ready \
        up down down-volumes test build push set-image install-k3s install-keda \
        secrets deploy delete status watch logs-api logs-worker logs-db \
        scale-worker scale-api pf-api pf-grafana pf-jaeger pf-flower
