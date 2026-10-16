FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
ENV NOTIFICATION_SERVICE_HOST=0.0.0.0
EXPOSE 8113
CMD ["python", "-c", "from notification_service.app import main_notification; main_notification()"]
