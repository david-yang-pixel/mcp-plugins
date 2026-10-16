FROM python:3.12-slim

WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --system --uid 10001 --no-create-home mcp
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENV ZENDESK_KG_MCP_HOST=0.0.0.0 ZENDESK_KG_MCP_PORT=8000
EXPOSE 8000
CMD ["zendesk-kg-mcp"]
