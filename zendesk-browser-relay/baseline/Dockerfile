FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libwayland-client0 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src

# Keep the Python Playwright package and its browser binary in the same image.
# The relay also mounts this path in production so browser downloads survive a
# container replacement and remain discoverable through PLAYWRIGHT_BROWSERS_PATH.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/zendesk-browser-relay/ms-playwright

RUN pip install --no-cache-dir . \
    && python -m playwright install chromium

ENV PYTHONPATH=/app/src
CMD ["sh", "-c", "python -m playwright install chromium && exec uvicorn zendesk_browser_relay.app:app --host 127.0.0.1 --port 8097"]
