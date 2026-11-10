# Running `ncp serve` in Docker

The repo's `Dockerfile` builds an image that runs `ncp serve` on port 4242 as a
non-root user. The SQLite store lives in `/data` (a declared volume), so
`/data/.ncp/store.db` survives container restarts.

## Build

```bash
docker build -t ncp-local:test .
```

The image installs `neural-context-protocol[pgvector,redis]` from the repo
source. `.dockerignore` keeps `.git`, `.ncp`, caches, tests, benchmarks, docs and
build output out of the build context.

## Run

`NCP_AUTH_TOKEN` must be supplied at run time. The image never contains a token.
The container refuses to start without one:

```bash
docker run -d --name ncp-server -e NCP_AUTH_TOKEN=<token> -p 127.0.0.1:4242:4242 -v ncp-data:/data ncp-local:test
```

Check it:

```bash
curl -s http://127.0.0.1:4242/healthz
docker inspect --format '{{.State.Health.Status}}' ncp-server
```

Without a token `ncp serve` refuses to bind a non-loopback host and the
container exits with status 2.

## Compose profile

The `ncp` service is behind the `server` profile, so a plain `docker compose up`
still starts only Postgres and Redis. Validate the file with:

```bash
docker compose --profile server config --services
```

Output (with the token set in the environment): `ncp`, `postgres`, `redis`.

To start the server through compose, export the token and run
`docker compose --profile server up -d ncp`. The service publishes
`127.0.0.1:4242:4242` and stores data in the `ncp-data` volume.

## Known limits

- The `ncp` service uses `${NCP_AUTH_TOKEN:-}` rather than a hard
  `${NCP_AUTH_TOKEN:?...}` check. Compose interpolates every service, including
  profile-gated ones, so a hard check would break the default `docker compose up`.
  The server still refuses to start without a token.
- The default config uses local embeddings, which the image does not install
  (`fastembed` is not in the requested extras). The server logs a warning and
  falls back to lexical-only retrieval.
