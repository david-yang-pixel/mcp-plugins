FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY . /app

RUN uv pip install --system -e .

ENV PYTHONPATH=/app/src

CMD ["python", "-c", "from zendesk_mcp.app import main; main()"]
