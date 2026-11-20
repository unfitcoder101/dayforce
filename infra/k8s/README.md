# Kubernetes

Kustomize manifests for the whole app: PostgreSQL with pgvector, the backend, the frontend.

```
infra/k8s/
  base/                  the manifests (images: dayforce-backend:step1, dayforce-frontend:step1)
  overlays/local/        namespace dayforce-local, images built on your machine
  overlays/main/         namespace main,       images from ghcr.io/unfitcoder101/dayforce-*
  overlays/production/   namespace production, images from ghcr.io/unfitcoder101/dayforce-*
  scripts/create-secret.sh   creates the namespace and the secrets from environment variables
```

How the pieces fit: the browser talks only to the **frontend** Service. The frontend image runs
nginx (`frontend/nginx.conf`): `/api/` is forwarded to the `backend` Service on port 8000 with the
path unchanged, everything else is the single-page app. The backend talks to the `postgres` Service.
Nothing is exposed outside the cluster; you reach the frontend with `kubectl port-forward`
(there is no Ingress yet).

## Secrets: nothing secret is in the repo

The manifests expect a Secret `postgres-secret` (key `POSTGRES_PASSWORD`) in the namespace.
`scripts/create-secret.sh` creates the namespace and that Secret from an environment variable:

```bash
POSTGRES_PASSWORD='pick-a-password' infra/k8s/scripts/create-secret.sh <namespace>
```

Changing the password later does not change the password inside an existing Postgres volume.

## The model: mock unless you change two things

The backend runs with `LLM_PROVIDER=mock` (see `base/backend-config.yaml`): fixed, offline answers,
no key, no network. It does **not** call a real model just because a key exists. To use a real model:

1. Add the key as a Secret: set `LLM_API_KEY` when you run `create-secret.sh` (it creates
   `llm-secret`), or `kubectl -n <namespace> create secret generic llm-secret --from-literal=LLM_API_KEY=...`.
   The backend Deployment reads it as `LLM_API_KEY` if the Secret exists.
2. Change `LLM_PROVIDER`, `LLM_BASE_URL` and `LLM_MODEL` in `backend-config` (for example
   `openai_compatible`, the provider's OpenAI-compatible URL, `openai/gpt-oss-20b`), then restart the
   backend: `kubectl -n <namespace> rollout restart deployment/backend`.

The rate limit `LLM_MAX_RPM` (default 5) is per backend replica, and the backend runs 2 to 6
replicas, so set it to the provider limit divided by the replica count.

## Option 1: kind or k3d on your machine (local images)

Needs Docker, `kubectl` and `kind` (or `k3d`).

```bash
kind create cluster --name timecard-local          # or: k3d cluster create timecard-local

docker build -t dayforce-backend:step1 -f backend/Dockerfile .
docker build -t dayforce-frontend:step1 ./frontend
kind load docker-image dayforce-backend:step1 dayforce-frontend:step1 --name timecard-local
#   k3d: k3d image import dayforce-backend:step1 dayforce-frontend:step1 -c timecard-local

POSTGRES_PASSWORD='pick-a-password' infra/k8s/scripts/create-secret.sh dayforce-local
make k8s-up                                        # kubectl apply -k infra/k8s/overlays/local, waits for rollouts
kubectl -n dayforce-local port-forward service/frontend 3000:8080
```

Open http://localhost:3000. Check the backend: `kubectl -n dayforce-local port-forward service/backend 8000:8000`
and `curl http://localhost:8000/health`.

Stop: `make k8s-down` scales the frontend and Postgres to zero and keeps the Postgres volume. The
backend autoscaler keeps at least 2 backend replicas; to stop them too, delete the namespace.

The autoscaler needs metrics-server. kind does not include it: install it
(`kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml`)
and, on kind only, add `--kubelet-insecure-tls` to its arguments. Never use that flag in production.

## Option 2: a VM with k3s (images from GHCR)

k3s is a single-binary Kubernetes with metrics-server built in. The CD workflow
(`.github/workflows/cd.yml`) pushes `ghcr.io/unfitcoder101/dayforce-backend` and
`dayforce-frontend` with the tags `latest` and `sha-<commit>` on every push to `main`.

On the VM (a Linux machine with `git` and `curl`):

```bash
curl -sfL https://get.k3s.io | sh -                # installs and starts k3s; read the script first if you prefer
sudo k3s kubectl get nodes                         # wait for Ready
git clone https://github.com/unfitcoder101/dayforce.git && cd dayforce
```

Let a normal `kubectl` use the k3s cluster, then create the namespace and secret (`main` or
`production`):

```bash
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown "$USER" ~/.kube/config
# (k3s ships its own kubectl too: `sudo k3s kubectl ...`; the script needs a plain `kubectl`)
POSTGRES_PASSWORD='pick-a-password' infra/k8s/scripts/create-secret.sh main
```

Pin both images to one commit and deploy (the tag is `sha-` plus the short commit id of a CD run
that has finished; details below):

```bash
cd infra/k8s/overlays/main            # or production
kustomize edit set image \
  dayforce-backend=ghcr.io/unfitcoder101/dayforce-backend:sha-<commit> \
  dayforce-frontend=ghcr.io/unfitcoder101/dayforce-frontend:sha-<commit>
kubectl kustomize . | grep image:     # check both lines show the sha tag
kubectl apply -k .
cd ../../../..
```

Then reach the app:

```bash
kubectl -n main get pods                           # wait until all are Ready
kubectl -n main port-forward service/frontend 3000:8080
#   from your laptop: ssh -L 3000:localhost:3000 <user>@<vm>   then open http://localhost:3000
```

If the GHCR packages are private, either make them public in the GitHub package settings or create
an image pull secret (`kubectl -n main create secret docker-registry ghcr --docker-server=ghcr.io
--docker-username=<user> --docker-password=<token with read:packages>`) and attach it to the
`default` service account of the namespace.

### About the image tags

The `main` and `production` overlays use the `latest` tag so that they render out of the box.
`latest` with `imagePullPolicy: IfNotPresent` can leave a node on an old image, so pin for a real
deploy. `kustomize` is the standalone tool: `kubectl` only has `kubectl kustomize`, which builds but
does not edit. Without it, change `newTag:` in the overlay's `kustomization.yaml` by hand. Commit
that change on a branch if you want the deployed version recorded in git.

## Verify without a cluster

```bash
kubectl kustomize infra/k8s/overlays/local      # also: main, production
```

## What is deployed

- PostgreSQL 16 with pgvector, a 1 GiB volume, the pgvector extension created at first start
- Backend: 2 replicas, probes on `/health`, resource requests and limits, an autoscaler (CPU 70%,
  2 to 6 replicas), `ENABLE_DEV_SEED=false`, mock model by default
- Frontend: nginx (unprivileged, port 8080) serving the app and proxying `/api/` to the backend,
  probes on `/`

## Not done

- No Ingress or public address; use `kubectl port-forward` or add an Ingress for your cluster.
- The Postgres image is `pgvector/pgvector:pg16`: the major version is pinned, not a digest.
- No login, so anyone who can reach the frontend can approve cases.
- Not run end to end on a cluster. Verified so far: `kubectl kustomize` for the three overlays, the
  frontend image (nginx proxy and single-page fallback, non-root) and `kustomize edit set image`.
