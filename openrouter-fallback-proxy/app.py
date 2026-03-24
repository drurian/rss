import os
from typing import cast

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
PLACEHOLDER_VALUE_MARKERS = (
    "replace-me",
    "changeme",
    "your-",
    "example",
    "placeholder",
)
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def is_missing_or_placeholder(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True

    lowered = normalized.lower()
    return any(marker in lowered for marker in PLACEHOLDER_VALUE_MARKERS)


def get_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


LLM_PROXY_API_KEY = os.environ.get("LLM_PROXY_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()


def using_explicit_proxy_config() -> bool:
    return bool(LLM_PROXY_API_KEY)


def using_groq_config() -> bool:
    return bool(GROQ_API_KEY)


def using_openrouter_config() -> bool:
    return bool(OPENROUTER_API_KEY)


API_KEY = get_env("LLM_PROXY_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY")

if using_explicit_proxy_config():
    API_URL = get_env(
        "LLM_PROXY_API_URL",
        "GROQ_API_URL",
        "OPENROUTER_API_URL",
        default="https://api.groq.com/openai/v1/chat/completions",
    )
    PRIMARY_MODEL = get_env(
        "LLM_PROXY_PRIMARY_MODEL",
        "GROQ_PRIMARY_MODEL",
        "OPENROUTER_PRIMARY_MODEL",
        default="openai/gpt-oss-20b",
    )
elif using_groq_config():
    API_URL = get_env("GROQ_API_URL", default="https://api.groq.com/openai/v1/chat/completions")
    PRIMARY_MODEL = get_env("GROQ_PRIMARY_MODEL", default="openai/gpt-oss-20b")
elif using_openrouter_config():
    API_URL = get_env("OPENROUTER_API_URL", default="https://openrouter.ai/api/v1/chat/completions")
    PRIMARY_MODEL = get_env("OPENROUTER_PRIMARY_MODEL", default="openrouter/free")
else:
    API_URL = get_env(
        "LLM_PROXY_API_URL",
        "GROQ_API_URL",
        "OPENROUTER_API_URL",
        default="https://api.groq.com/openai/v1/chat/completions",
    )
    PRIMARY_MODEL = get_env(
        "LLM_PROXY_PRIMARY_MODEL",
        "GROQ_PRIMARY_MODEL",
        "OPENROUTER_PRIMARY_MODEL",
        default="openai/gpt-oss-20b",
    )

TIMEOUT = float(get_env("LLM_PROXY_TIMEOUT", "GROQ_TIMEOUT", "OPENROUTER_TIMEOUT", default="120"))
SITE_URL = get_env("LLM_PROXY_SITE_URL", "OPENROUTER_SITE_URL", default="")
APP_NAME = get_env("LLM_PROXY_APP_NAME", "OPENROUTER_APP_NAME", default="rss-miniflux-ai")
PROXY_DEFAULT_MODEL = "proxy/default"


def validate_settings() -> None:
    if is_missing_or_placeholder(API_KEY):
        raise RuntimeError(
            "LLM_PROXY_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY must be set to a real API key"
        )


validate_settings()


def parse_fallback_models() -> list[str]:
    if using_explicit_proxy_config():
        raw = get_env(
            "LLM_PROXY_FALLBACK_MODELS",
            "GROQ_FALLBACK_MODELS",
            "OPENROUTER_FALLBACK_MODELS",
            default="",
        )
    elif using_groq_config():
        raw = get_env("GROQ_FALLBACK_MODELS", default="")
    elif using_openrouter_config():
        raw = get_env(
            "OPENROUTER_FALLBACK_MODELS",
            default="meta-llama/llama-3.3-70b-instruct:free,qwen/qwen3-next-80b-a3b-instruct:free,openai/gpt-oss-20b:free",
        )
    else:
        raw = get_env(
            "LLM_PROXY_FALLBACK_MODELS",
            "GROQ_FALLBACK_MODELS",
            "OPENROUTER_FALLBACK_MODELS",
            default="",
        )
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    if SITE_URL:
        headers["HTTP-Referer"] = SITE_URL
    if APP_NAME:
        headers["X-Title"] = APP_NAME

    return headers


def resolve_requested_model(payload: dict) -> str:
    requested_model = str(payload.get("model", "")).strip()
    if requested_model and requested_model != PROXY_DEFAULT_MODEL:
        return requested_model
    return PRIMARY_MODEL


def build_model_sequence(payload: dict) -> list[str]:
    sequence = [resolve_requested_model(payload), *parse_fallback_models()]
    seen = set()
    deduped = []
    for model in sequence:
        if model and model not in seen:
            deduped.append(model)
            seen.add(model)
    return deduped


def build_attempt_payload(payload: dict, model: str) -> dict:
    attempt_payload = dict(payload)
    # The local proxy owns model routing. Ignore any upstream multi-model hint.
    attempt_payload.pop("models", None)
    attempt_payload["model"] = model
    return attempt_payload


def error_response(message: str, status_code: int) -> Response:
    response = jsonify({"error": message})
    response.status_code = status_code
    return response


def forward_with_fallback(payload: dict) -> requests.Response | Response:
    models = build_model_sequence(payload)
    last_response: requests.Response | None = None

    for index, model in enumerate(models, start=1):
        attempt_payload = build_attempt_payload(payload, model)
        app.logger.info(
            "forwarding chat completion attempt=%s model=%s fallback_count=%s requested_model=%s",
            index,
            model,
            max(len(models) - 1, 0),
            payload.get("model"),
        )

        try:
            response = requests.post(
                API_URL,
                headers=build_headers(),
                json=attempt_payload,
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            app.logger.warning(
                "upstream request error model=%s attempt=%s error=%s",
                model,
                index,
                exc,
            )
            continue

        last_response = response

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response

        app.logger.warning(
            "retryable upstream error status=%s model=%s attempt=%s",
            response.status_code,
            model,
            index,
        )

    if last_response is None:
        return error_response("all fallback attempts failed before receiving a response", 502)

    return last_response


@app.post("/v1/chat/completions")
def chat_completions():
    if not API_KEY:
        return jsonify({"error": "missing LLM_PROXY_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY"}), 500

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    response = forward_with_fallback(payload)
    if isinstance(response, Response):
        return response
    response = cast(requests.Response, response)

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
            "has_api_key": bool(API_KEY),
            "api_url": API_URL,
            "primary_model": PRIMARY_MODEL,
            "fallback_models": parse_fallback_models(),
        }
    ), 200
