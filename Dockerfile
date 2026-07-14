FROM python:3.12-slim

WORKDIR /app
ARG DENO_VERSION=2.9.1
ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
  && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
  case "${TARGETARCH:-amd64}" in \
    amd64) deno_arch="x86_64" ;; \
    arm64) deno_arch="aarch64" ;; \
    *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
  esac; \
  deno_base="https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}-unknown-linux-gnu.zip"; \
  python - "$deno_base" <<'PY'
import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

base_url = sys.argv[1]
zip_path = Path("/tmp/deno.zip")
sha_url = f"{base_url}.sha256sum"
zip_path.write_bytes(urllib.request.urlopen(base_url, timeout=60).read())
expected = urllib.request.urlopen(sha_url, timeout=60).read().decode("utf-8").split()[0]
actual = hashlib.sha256(zip_path.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit("deno checksum mismatch")
with zipfile.ZipFile(zip_path) as archive:
    archive.extract("deno", "/usr/local/bin")
zip_path.unlink(missing_ok=True)
PY
RUN chmod +x /usr/local/bin/deno \
  && deno --version

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt yt-dlp

COPY . /app

CMD ["python", "manage.py", "media_worker"]
