#!/usr/bin/env python3

import os
import re
import sys
from pathlib import Path


REQUIRED_ENV_VARS = (
    "MINIFLUX_BASE_URL",
    "MINIFLUX_API_KEY",
    "MINIFLUX_AI_POLL_INTERVAL",
    "MINIFLUX_AI_PROVIDER_BASE_URL",
    "MINIFLUX_AI_PROVIDER_API_KEY",
    "MINIFLUX_AI_MODEL",
    "MINIFLUX_AI_TIMEOUT",
    "MINIFLUX_AI_MAX_WORKERS",
    "MINIFLUX_AI_RPM",
)

PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
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


def replace_known_placeholders(template: str) -> str:
    values = {}
    missing = []

    for name in REQUIRED_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if is_missing_or_placeholder(value):
            missing.append(name)
        values[name] = value

    if missing:
        missing_list = ", ".join(sorted(missing))
        raise SystemExit(f"Missing required environment variables: {missing_list}")

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return values.get(name, match.group(0))

    return PLACEHOLDER_RE.sub(repl, template)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: bootstrap_config.py TEMPLATE OUTPUT")

    template_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    rendered = replace_known_placeholders(template_path.read_text())
    output_path.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
