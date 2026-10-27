FROM python:3.12-slim
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONUNBUFFERED=1 BINANCE_MCP_HOST=0.0.0.0 BINANCE_MCP_PORT=8124
EXPOSE 8124
CMD ["binance-mcp"]
