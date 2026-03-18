import hashlib
import hmac
import os
import shlex
import subprocess

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SECRET = os.environ.get("MINIFLUX_WEBHOOK_SECRET", "")
ARCHIVEBOX_CONTAINER_NAME = os.environ.get("ARCHIVEBOX_CONTAINER_NAME", "rss-archivebox-1")


def is_valid_signature(raw_body: bytes, provided_signature: str) -> bool:
    if not SECRET:
        return False

    expected_signature = hmac.new(
        SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_signature, provided_signature or "")


def extract_url(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None

    entry = payload.get("entry")
    if isinstance(entry, dict):
        return entry.get("url")

    entries = payload.get("entries")
    if isinstance(entries, list) and entries:
        first = entries[0]
        if isinstance(first, dict):
            return first.get("url")

    return payload.get("url")


@app.post("/miniflux-archivebox")
def miniflux_archivebox():
    raw_body = request.get_data()
    signature = request.headers.get("X-Miniflux-Signature", "")
    event_type = request.headers.get("X-Miniflux-Event-Type", "")

    if not is_valid_signature(raw_body, signature):
        abort(401, "invalid signature")

    if event_type != "save_entry":
        return jsonify({"status": "ignored", "reason": "unsupported event type"}), 200

    payload = request.get_json(silent=True) or {}
    url = extract_url(payload)

    if not url:
        return jsonify({"status": "ignored", "reason": "missing url"}), 200

    cmd = [
        "docker",
        "exec",
        "--user=archivebox",
        ARCHIVEBOX_CONTAINER_NAME,
        "/bin/bash",
        "-lc",
        f"archivebox add {shlex.quote(url)}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        app.logger.error("archivebox add failed: %s", result.stderr[-2000:])
        return (
            jsonify(
                {
                    "status": "error",
                    "url": url,
                    "returncode": result.returncode,
                    "stderr": result.stderr[-2000:],
                }
            ),
            500,
        )

    app.logger.info("archived url=%s", url)
    return jsonify({"status": "archived", "url": url}), 200


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True}), 200
