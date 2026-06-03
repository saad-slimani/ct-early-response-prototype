# Render Deployment Guide

This repo is prepared for a Render web service deploy.

## What Is Included

- `render.yaml` for a Git-backed Render Blueprint deploy
- `runtime.txt` to pin Python 3.11
- `APP_STORAGE_ROOT` support so runtime data can live outside the source tree
- a free Render Postgres database for workflow metadata

## Current Service Shape

- Type: `web`
- Runtime: `python`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/healthz`
- Database: free Render Postgres via `DATABASE_URL`

## Deploy Steps

1. Push this repo to GitHub, GitLab, or Bitbucket.
2. In Render, create a new Blueprint from the repo.
3. Confirm the service name and branch.
4. Deploy.

## Important Storage Note

The current Blueprint is free-tier compatible:

- workflow metadata is stored in free Render Postgres
- uploaded studies, masks, and trained models still live on the web service filesystem

Because Render Free web services use an ephemeral filesystem, uploaded studies, masks, and trained models can be lost on restart, redeploy, or service spin-down. This is acceptable for a demo MVP, but not for production or durable pilot use.

For a durable deployment:

1. Change the service plan from `free` to a paid web-service plan such as `starter`.
2. Attach a persistent disk in the Render Dashboard.
3. Mount the disk at `/var/data`.
4. Change `APP_STORAGE_ROOT` from `/tmp/ct-early-response` to `/var/data/ct-early-response`.

You can keep the same application code when making that switch.

## After First Deploy

Check:

- `/healthz` returns `{"status":"ok"}`
- static UI loads at `/`
- study uploads work
- segmentation jobs can write masks under the configured storage root
- worklist/report state survives web service restarts because it lives in Postgres
