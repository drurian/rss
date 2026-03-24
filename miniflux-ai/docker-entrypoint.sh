#!/bin/sh
set -eu

python /app/bootstrap_config.py /app/config.template.yml /app/config.yml
exec python /app/main.py
