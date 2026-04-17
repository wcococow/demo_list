REGISTRY   ?= yourname
IMAGE      ?= task-api
TAG        ?= latest
NAMESPACE  ?= task-api
FULL_IMAGE  = $(REGISTRY)/$(IMAGE):$(TAG)

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
	curl -sfL https://get.k3s.io | sh -
	mkdir -p ~/.kube
	sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
	sudo chown $(USER) ~/.kube/config

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
	kubectl apply -f k8s/ --namespace=$(NAMESPACE)

delete:
	kubectl delete -f k8s/ --namespace=$(NAMESPACE)

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

.PHONY: up down down-volumes test build push set-image install-k3s install-keda \
        secrets deploy delete status watch logs-api logs-worker logs-db \
        scale-worker scale-api pf-api pf-grafana pf-jaeger pf-flower
