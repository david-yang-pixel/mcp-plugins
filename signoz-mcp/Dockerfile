FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
RUN chmod +x /app/scripts/sz
ENV SIGNOZ_MCP_SZ_PATH=/app/scripts/sz
ENV SIGNOZ_MCP_HOST=0.0.0.0
EXPOSE 8098
CMD ["python", "-c", "from signoz_mcp.app import main; main()"]
