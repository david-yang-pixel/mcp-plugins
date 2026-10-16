FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
ENV SOURCE_MCP_HOST=0.0.0.0
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=3)); raise SystemExit(0 if d.get('ok') else 1)"]
CMD ["python", "-c", "from source_mcp.app import main; main()"]
