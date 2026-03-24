FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask gunicorn requests

COPY app.py /app/app.py

EXPOSE 8081

CMD ["gunicorn", "--bind", "0.0.0.0:8081", "app:app"]
