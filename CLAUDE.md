# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Docker Compose stack for self-hosted RSS reading, bookmark management, and web archiving. The services are:

- **Miniflux** (v2.2.16) — RSS feed reader (port 8080)
- **Linkwarden** (v2.13.5) — Bookmark manager with Meilisearch full-text search (port 3000)
- **ArchiveBox** — Web page archiver (port 8000)
- **archivebox-webhook** — Custom Flask/Gunicorn microservice that receives Miniflux webhook events and archives URLs into ArchiveBox (port 8090)

Each web service is exposed via Traefik reverse proxy with automatic Let's Encrypt TLS on the `astribelli.com` domain.

## Architecture

The key integration: Miniflux fires HMAC-signed webhooks on `save_entry` events → the Flask webhook service (`archivebox-webhook/app.py`) validates the signature, extracts the URL, and runs `docker exec` into the ArchiveBox container to archive it. The webhook container mounts the Docker socket read-only to execute commands in sibling containers.

Miniflux and Linkwarden each have their own Postgres database. Linkwarden also uses Meilisearch for indexing.

## Commands

```bash
# Start all services (production, behind Traefik)
docker compose up -d

# Start with local port exposure (development)
# docker-compose.override.yml exposes ports directly
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d

# Rebuild the webhook service after code changes
docker compose build archivebox-webhook
docker compose up -d archivebox-webhook

# View webhook logs
docker compose logs -f archivebox-webhook
```

## Environment

All secrets and configuration are in `.env` (gitignored). Required variables are documented in the `.env` file itself with recommended lengths. Key variables: database passwords, Miniflux admin credentials, Linkwarden NextAuth/Meilisearch secrets, webhook HMAC secret, and ArchiveBox CSRF origins.

## Networking

Services communicate on Docker's default network. Web-facing services (miniflux, linkwarden, archivebox) also join the external `pokemoncollector_proxy` network for Traefik routing. The webhook service is internal-only, exposed on port 8090.
