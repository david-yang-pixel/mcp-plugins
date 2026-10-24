FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium
EXPOSE 8097
ENV UVICORN_HOST=0.0.0.0
# Liveness of the HTTP process only; browser login state stays on /ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('UVICORN_PORT', '8097') + '/health', timeout=4)"]
CMD ["python", "-m", "zendesk_browser_relay.app"]
