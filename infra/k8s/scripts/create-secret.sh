#!/usr/bin/env bash
# Create the namespace and the secrets the manifests expect. Values come from environment
# variables, never from a file in the repo.
#
#   POSTGRES_PASSWORD='choose-one' infra/k8s/scripts/create-secret.sh main
#   POSTGRES_PASSWORD='choose-one' LLM_API_KEY='your-key' infra/k8s/scripts/create-secret.sh production
#
# Creates (or updates): namespace <namespace>, secret postgres-secret (POSTGRES_PASSWORD), and,
# only if LLM_API_KEY is set, secret llm-secret (LLM_API_KEY).
# The password is passed on stdin, not on the command line. Changing it later does NOT change the
# password inside an existing Postgres volume.
set -euo pipefail

NS="${1:?usage: POSTGRES_PASSWORD=... $0 <namespace>}"
: "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in the environment}"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

printf '%s' "$POSTGRES_PASSWORD" |
  kubectl -n "$NS" create secret generic postgres-secret \
    --from-file=POSTGRES_PASSWORD=/dev/stdin --dry-run=client -o yaml |
  kubectl apply -f -

if [ -n "${LLM_API_KEY:-}" ]; then
  printf '%s' "$LLM_API_KEY" |
    kubectl -n "$NS" create secret generic llm-secret \
      --from-file=LLM_API_KEY=/dev/stdin --dry-run=client -o yaml |
    kubectl apply -f -
  echo "created llm-secret (the backend only uses a real model if backend-config says so; see infra/k8s/README.md)"
fi

echo "namespace $NS and postgres-secret are ready"
