import hashlib
import hmac
import os
import subprocess

from flask import Flask, abort, jsonify, request

app = Flask(__name__)

SECRET = os.environ.get("MINIFLUX_WEBHOOK_SECRET", "")
ARCHIVEBOX_DATA_DIR = os.environ.get("ARCHIVEBOX_DATA_DIR", "/archivebox-data")


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

    # Be tolerant about payload shape.
    if isinstance(payload.get("entry"), dict):
        return payload["entry"].get("url")

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
        "run",
        "--rm",
        "-v",
        f"{ARCHIVEBOX_DATA_DIR}:/data",
        "archivebox/archivebox:stable",
        "add",
        url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return (
            jsonify(
                {
                    "status": "error",
                    "url": url,
                    "returncode": result.returncode,
                    "stderr": result.stderr[-4000:],
                }
            ),
            500,
        )

    return jsonify({"status": "archived", "url": url}), 200


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True}), 200
