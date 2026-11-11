FROM python:3.12-slim

# Upgrade: set both values from the release's difyctl-v<version>-checksums.txt, then rebuild.
ARG DIFY_VERSION=1.19.0
ARG DIFYCTL_SHA256=3c855fd9d225950518c97a561c055da73e3ed632118553329441442c6a8cf00d

WORKDIR /app
RUN python -c "import hashlib, sys, urllib.request; version, expected = sys.argv[1:]; \
url = f'https://github.com/langgenius/dify/releases/download/{version}/difyctl-v{version}-linux-x64'; \
data = urllib.request.urlopen(url, timeout=300).read(); \
actual = hashlib.sha256(data).hexdigest(); \
sys.exit(f'difyctl checksum mismatch: {actual}') if actual != expected else open('/usr/local/bin/difyctl', 'wb').write(data)" \
    "$DIFY_VERSION" "$DIFYCTL_SHA256" \
 && chmod 0755 /usr/local/bin/difyctl \
 && mkdir -p /opt/difyctl/skills \
 && HOME=/tmp/difyctl-build DIFY_CONFIG_DIR=/tmp/difyctl-build difyctl install skills /opt/difyctl/skills --json \
 && rm -rf /tmp/difyctl-build \
 && difyctl version --json
COPY requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
 && useradd --system --uid 10001 --no-create-home mcp \
 && mkdir -p /var/lib/mcp/difyctl \
 && chown 10001:10001 /var/lib/mcp/difyctl \
 && chmod 0700 /var/lib/mcp/difyctl
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DIFYCTL_MCP_HOST=0.0.0.0 DIFYCTL_MCP_PORT=8000 DIFYCTL_MCP_DATA_DIR=/var/lib/mcp/difyctl \
    DIFYCTL_BIN=/usr/local/bin/difyctl DIFYCTL_SKILLS_DIR=/opt/difyctl/skills DIFY_VERSION=${DIFY_VERSION}
EXPOSE 8000
CMD ["difyctl-mcp"]
