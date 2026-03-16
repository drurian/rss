FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask gunicorn

COPY app.py /app/app.py

EXPOSE 8090

CMD ["gunicorn", "--bind", "0.0.0.0:8090", "app:app"]
