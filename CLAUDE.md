# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Docker Compose stack for self-hosted RSS reading, AI summaries, bookmark management, and web archiving. The services are:

- **Miniflux** (v2.2.16) — RSS feed reader (port 8080)
- **openrouter-fallback-proxy** — Internal OpenAI-compatible proxy that currently routes summary requests to Groq and retries across a configured model fallback list
- **entry-summarizer** — Custom summarizer that polls unread Miniflux entries, summarizes normal articles, and fetches YouTube transcripts before summarizing videos
- **miniflux-ai** — Legacy companion service kept behind a Compose profile for rollback only
- **Linkwarden** (v2.13.5) — Bookmark manager with Meilisearch full-text search (port 3000)
- **ArchiveBox** — Web page archiver (port 8000)
- **archivebox-webhook** — Custom Flask/Gunicorn microservice that receives Miniflux webhook events and archives URLs into ArchiveBox (port 8090)

Each web service is exposed via Traefik reverse proxy with automatic Let's Encrypt TLS on the `astribelli.com` domain.

## Architecture

The key integrations:

- Miniflux fires HMAC-signed webhooks on `save_entry` events → the Flask webhook service (`archivebox-webhook/app.py`) validates the signature, extracts the URL, and runs `docker exec` into the ArchiveBox container to archive it. The webhook container mounts the Docker socket read-only to execute commands in sibling containers.
- `entry-summarizer` polls Miniflux over the API, writes summaries back into entry content, and keeps durable per-entry state in SQLite so already-processed entries are not retried every poll.
- For YouTube URLs, `entry-summarizer` fetches captions with `youtube-transcript-api` and summarizes the transcript instead of the RSS entry body. Transcript fetching is intentionally conservative: one fetch worker, one fetch every 10 minutes max by default, 2-5 seconds of jitter per fetch attempt, and persistent cooldown when YouTube blocks the VPS IP.
- `openrouter-fallback-proxy` sits in front of the upstream OpenAI-compatible API so the summarizer can keep using a single OpenAI client while the proxy retries sequentially across a configured Groq model fallback list on retryable upstream failures such as `429`.
- `miniflux-ai` remains available only under the `legacy` Compose profile for rollback. Do not run it alongside `entry-summarizer`; both services write into Miniflux entry content and will race.

Miniflux and Linkwarden each have their own Postgres database. Linkwarden also uses Meilisearch for indexing.

## Commands

```bash
# Start all services (production, behind Traefik)
docker compose --profile summaries up -d

# Start with local port exposure (development)
# docker-compose.override.yml exposes ports directly
docker compose --profile summaries -f docker-compose.yml -f docker-compose.override.yml up -d

# Rebuild the webhook service after code changes
docker compose build archivebox-webhook
docker compose up -d archivebox-webhook

# Start or refresh the active summarizer
docker compose --profile summaries up -d entry_summarizer

# Purge tracked non-YouTube summaries and keep them suppressed until the article body changes
docker compose --profile summaries run --rm -e ENTRY_SUMMARIZER_COMMAND=purge-articles entry_summarizer

# Start or refresh the internal LLM proxy
docker compose up -d openrouter_fallback_proxy

# Start the legacy miniflux-ai service only for rollback testing
docker compose --profile legacy up -d miniflux_ai

# View webhook logs
docker compose logs -f archivebox-webhook

# View AI summarizer logs
docker compose logs -f entry_summarizer

# View internal LLM proxy logs
docker compose logs -f openrouter_fallback_proxy
```

## Environment

All secrets and configuration are in `.env` (gitignored). Required variables are documented in the `.env` file itself with recommended lengths. Key variables: database passwords, Miniflux admin credentials, Miniflux API key, a Groq or `LLM_PROXY_*` API key for the fallback proxy, the proxy primary/fallback model list, the internal provider URL/model/API key used by `entry-summarizer`, `ENTRY_SUMMARIZER_COMMAND` for one-shot maintenance tasks such as purging article summaries, the YouTube transcript pacing/backoff settings, Linkwarden NextAuth/Meilisearch secrets, webhook HMAC secret, and ArchiveBox CSRF origins.

## Networking

Services communicate on Docker's default network. Web-facing services (miniflux, linkwarden, archivebox) also join the external `pokemoncollector_proxy` network for Traefik routing. `entry-summarizer`, `miniflux-ai`, and `openrouter-fallback-proxy` stay internal-only and talk to Miniflux and the upstream LLM provider over the default network. The webhook service is internal-only, exposed on port 8090.
