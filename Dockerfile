FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=deb.debian.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN if [ "${DEBIAN_MIRROR}" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 bale \
    && useradd --uid 10001 --gid bale --create-home --shell /usr/sbin/nologin bale \
    && mkdir -p /workspace /data /config \
    && chown -R bale:bale /workspace /data /config

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --index-url "${PIP_INDEX_URL}" .

USER bale

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["uvicorn", "bale_harness.http:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
