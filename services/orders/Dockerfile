FROM python:3.12-slim

# procps (ps/top), dnsutils (dig/nslookup) y curl para diagnosticar dentro del contenedor
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps dnsutils curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt py-spy==0.4.0

COPY orders ./orders

RUN useradd --uid 10001 --create-home app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data
USER app

EXPOSE 8000
# Un solo proceso uvicorn: simplifica las métricas y hace visible el bloqueo del GIL (cpu-spike)
CMD ["uvicorn", "orders.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
