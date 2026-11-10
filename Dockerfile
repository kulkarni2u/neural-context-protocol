FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir '.[pgvector,redis]' \
 && rm -rf /src

RUN groupadd --system ncp \
 && useradd --system --gid ncp --home-dir /data --shell /usr/sbin/nologin ncp \
 && mkdir -p /data \
 && chown ncp:ncp /data

USER ncp
WORKDIR /data
VOLUME ["/data"]
EXPOSE 4242

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4242/healthz', timeout=4).status == 200 else 1)"

# ncp serve refuses to bind a non-loopback host without a token, so
# NCP_AUTH_TOKEN must be supplied at run time; it is never baked into the image.
CMD ["ncp", "serve", "--host", "0.0.0.0", "--port", "4242"]
