# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Docker Compose stack for self-hosted RSS reading, AI summaries, bookmark management, and web archiving. The services are:

- **Miniflux** (v2.2.16) — RSS feed reader (port 8080)
- **openrouter-fallback-proxy** — Internal OpenAI-compatible proxy that sends summary requests to OpenRouter with free-model routing and fallback models
- **miniflux-ai** — Companion service that polls Miniflux and writes AI-generated summaries back into entry content
- **Linkwarden** (v2.13.5) — Bookmark manager with Meilisearch full-text search (port 3000)
- **ArchiveBox** — Web page archiver (port 8000)
- **archivebox-webhook** — Custom Flask/Gunicorn microservice that receives Miniflux webhook events and archives URLs into ArchiveBox (port 8090)

Each web service is exposed via Traefik reverse proxy with automatic Let's Encrypt TLS on the `astribelli.com` domain.

## Architecture

The key integrations:

- Miniflux fires HMAC-signed webhooks on `save_entry` events → the Flask webhook service (`archivebox-webhook/app.py`) validates the signature, extracts the URL, and runs `docker exec` into the ArchiveBox container to archive it. The webhook container mounts the Docker socket read-only to execute commands in sibling containers.
- `miniflux-ai` polls Miniflux over the API every few minutes, summarizes unread entries, and updates the entry content in Miniflux so summaries can appear in both the Miniflux web UI and Android clients such as Read You.
- `openrouter-fallback-proxy` sits in front of OpenRouter's OpenAI-compatible API so `miniflux-ai` can keep using a single OpenAI client while still sending requests through OpenRouter's free-model router with explicit fallback models.
- The compose defaults are intentionally conservative for free-model usage: `miniflux-ai` defaults to one worker and one request per minute unless overridden in `.env`.

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

# Start or refresh the AI summarizer
docker compose up -d miniflux_ai

# Start or refresh the OpenRouter proxy
docker compose up -d openrouter_fallback_proxy

# View webhook logs
docker compose logs -f archivebox-webhook

# View AI summarizer logs
docker compose logs -f miniflux_ai

# View OpenRouter proxy logs
docker compose logs -f openrouter_fallback_proxy
```

## Environment

All secrets and configuration are in `.env` (gitignored). Required variables are documented in the `.env` file itself with recommended lengths. Key variables: database passwords, Miniflux admin credentials, Miniflux API key, OpenRouter API key, OpenRouter primary/fallback model list, the internal provider URL/model/API key used by `miniflux-ai`, Linkwarden NextAuth/Meilisearch secrets, webhook HMAC secret, and ArchiveBox CSRF origins.

## Networking

Services communicate on Docker's default network. Web-facing services (miniflux, linkwarden, archivebox) also join the external `pokemoncollector_proxy` network for Traefik routing. `miniflux-ai` and `openrouter-fallback-proxy` stay internal-only and talk to Miniflux/OpenRouter over the default network. The webhook service is internal-only, exposed on port 8090.
