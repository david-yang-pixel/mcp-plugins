# tsh for plugin_lambda_logs / kube_read only. The host-mounted /usr/local/bin/tsh
# (18.0.0) fails `tsh aws` signature verification against the 18.11 proxy, so the
# image carries its own copy at /opt/teleport/bin/tsh (outside the mounted path).
FROM python:3.12-slim AS tsh
ARG TELEPORT_VERSION=18.11.0
ARG TELEPORT_SHA256=8d3dba03ea6b6e0d72c97e190da0595e8f0d70f5573c3a26b1908cf60caf6e3f
RUN python -c "import hashlib, os, tarfile, urllib.request; \
url = 'https://cdn.teleport.dev/teleport-v${TELEPORT_VERSION}-linux-amd64-bin.tar.gz'; \
digest = hashlib.sha256(); out = open('/tmp/teleport.tgz', 'wb'); \
resp = urllib.request.urlopen(url, timeout=300); \
[(digest.update(chunk), out.write(chunk)) for chunk in iter(lambda: resp.read(1 << 20), b'')]; \
out.close(); \
assert digest.hexdigest() == '${TELEPORT_SHA256}', 'sha256 mismatch: ' + digest.hexdigest(); \
archive = tarfile.open('/tmp/teleport.tgz'); member = archive.getmember('teleport/tsh'); \
member.name = 'tsh'; os.makedirs('/out', exist_ok=True); archive.extract(member, '/out', filter='data')" \
    && chmod 0755 /out/tsh && rm /tmp/teleport.tgz && /out/tsh version

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps .
COPY --from=tsh /out/tsh /opt/teleport/bin/tsh
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
ENV DB_GATEWAY_LISTEN_HOST=0.0.0.0
EXPOSE 18081
CMD ["python", "-c", "from db_gateway.app import run_db_gateway_app; run_db_gateway_app()"]
