# Deploy on Render (free tier)

The whole app runs on Render's free tier from one file, `render.yaml` (a Render "Blueprint").
It creates three things:

| Render resource | What it is | Plan |
| --- | --- | --- |
| `timecard-db` | PostgreSQL 16 (the backend turns on the `vector` extension itself) | free |
| `timecard-agent-api` | The FastAPI backend, built from `backend/Dockerfile` | free |
| `timecard-agent` | The React frontend as a static site | free |

The demo uses the mock model (`LLM_PROVIDER=mock`), so no API key is needed. No password or key
is written in any file: Render passes the database address to the backend as `DATABASE_URL`.

## Deploy it

1. Sign in at render.com (a GitHub sign-in is easiest) and allow Render to read this repository.
2. In the Render Dashboard click **New → Blueprint** and pick this repository. Choose the branch
   that has `render.yaml` (`main` once it is merged).
3. Render lists the three resources from `render.yaml`. Click **Apply** (or **Deploy Blueprint**).
4. Wait. The database is ready in a few minutes. The backend build is the slow part (about
   10 to 20 minutes the first time: it installs the Python packages and downloads an embedding
   model into the image). The static site builds in a minute or two.
5. Check the backend: open `https://<backend address>/health`. It should answer
   `{"status": "ok", "database": "ok"}`.
6. Check the backend address. The frontend forwards `/api/*` to
   `https://timecard-agent-api.onrender.com`. If Render gave the backend a different address
   (shown at the top of the backend's page, for example with a few extra letters), put the real
   address in `render.yaml` under the static site's `routes`, or change it in the Render Dashboard
   under the static site's **Redirects/Rewrites**, then redeploy the static site.
7. Open the frontend address. The top bar should say **Live data**.

## Load the demo data

A new database has no cases. With the app deployed, run from the repo root on your own machine
(needs `curl` and `python3`):

```bash
API=https://<frontend address> scripts/demo_seed.sh
```

This sends four known timecards through `POST /api/analysis/run` and gives one open case of each
exception type. Do **not** use `--reset` here: it empties a local Docker database and does not work
against Render. Open the app (or `/health` on the backend) first if the backend has been idle, so
the script does not have to wait for it to wake up. Running the script twice is safe: the second
run says the timecards are already there.

`POST /api/dev/seed` stays switched off (`ENABLE_DEV_SEED=false`), as in every deployment.

## Known limits of the free tier

- **The backend sleeps.** After about 15 minutes with no traffic Render stops it. The next request
  wakes it, which takes about a minute, so the first page load after a pause is slow (or shows an
  error once; reload after a minute).
- **The free database expires** 30 days after it is created (Render then gives 14 days to upgrade
  before deleting it). It also has a fixed 1 GB of storage, far more than the demo needs.
- **API calls take one extra hop.** The static site forwards `/api/*` to the backend's public
  address, which adds some delay to each call.
- **Build minutes.** Free builds use the workspace's monthly build minutes. The backend image is
  large, so avoid needless redeploys.
- **One copy of the backend.** The free plan runs one instance, so the 2 to 6 replica autoscaling
  from `infra/k8s/` does not apply here.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| The app says "Something went wrong" right after the first deploy | The backend started before the database was ready. It now waits up to 30 seconds (`DB_INIT_RETRY_SECONDS`), but if it still happens, open the backend in the Dashboard and click **Manual Deploy → Restart service** once. |
| The page loads but the list stays empty or shows an error | Check `/health` on the backend. If it is asleep, wait a minute and reload. If it says the database is unreachable, restart the backend. |
| `/api/...` calls answer 404 from the static site | The rewrite points at the wrong backend address. See step 6. |
| The backend deploy fails with "no open ports detected" | Check that `PORT` is `8000` in the backend's environment (it comes from `render.yaml`). |
| The demo data script says HTTP 5xx | The backend was still waking up. Run it again after a minute. |
