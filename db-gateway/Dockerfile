FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
ENV DB_GATEWAY_LISTEN_HOST=0.0.0.0
EXPOSE 18081
CMD ["python", "-c", "from db_gateway.app import run_db_gateway_app; run_db_gateway_app()"]
