import os

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
app.config["TRUSTED_HOSTS"] = [
    "openrouter-fallback-proxy",
    "openrouter-fallback-proxy:8081",
    "127.0.0.1",
    "127.0.0.1:8081",
    "localhost",
    "localhost:8081",
]

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = os.environ.get(
    "OPENROUTER_API_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)
OPENROUTER_PRIMARY_MODEL = os.environ.get("OPENROUTER_PRIMARY_MODEL", "openrouter/free")
OPENROUTER_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT", "120"))
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "").strip()
OPENROUTER_APP_NAME = os.environ.get("OPENROUTER_APP_NAME", "rss-miniflux-ai").strip()
PLACEHOLDER_VALUE_MARKERS = (
    "replace-me",
    "changeme",
    "your-",
    "example",
    "placeholder",
)


def is_missing_or_placeholder(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True

    lowered = normalized.lower()
    return any(marker in lowered for marker in PLACEHOLDER_VALUE_MARKERS)


def validate_settings() -> None:
    if is_missing_or_placeholder(OPENROUTER_API_KEY):
        raise RuntimeError("OPENROUTER_API_KEY must be set to a real OpenRouter API key")


validate_settings()


def parse_fallback_models() -> list[str]:
    raw = os.environ.get("OPENROUTER_FALLBACK_MODELS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = OPENROUTER_APP_NAME

    return headers


@app.post("/v1/chat/completions")
def chat_completions():
    if not OPENROUTER_API_KEY:
        return jsonify({"error": "missing OPENROUTER_API_KEY"}), 500

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid JSON body"}), 400

    payload.setdefault("model", OPENROUTER_PRIMARY_MODEL)

    fallback_models = parse_fallback_models()
    if fallback_models and "models" not in payload:
        payload["models"] = fallback_models

    app.logger.info(
        "forwarding chat completion primary_model=%s fallback_count=%s",
        payload.get("model"),
        len(payload.get("models", [])),
    )

    response = requests.post(
        OPENROUTER_API_URL,
        headers=build_headers(),
        json=payload,
        timeout=OPENROUTER_TIMEOUT,
    )

    return Response(
        response.content,
        status=response.status_code,
        content_type=response.headers.get("Content-Type", "application/json"),
    )


@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "has_api_key": bool(OPENROUTER_API_KEY),
            "primary_model": OPENROUTER_PRIMARY_MODEL,
            "fallback_models": parse_fallback_models(),
        }
    ), 200
