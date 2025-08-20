FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
ca-certificates curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ./ /app
WORKDIR /app

# Default to daemon mode
CMD ["python", "-u", "monitor_serp.py", "serve"]