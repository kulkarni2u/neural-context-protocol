# Security

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's private vulnerability
reporting for this repository: open the repository's **Security** tab and
choose **Report a vulnerability**. Please do not open a public issue or pull
request for an unpatched vulnerability.

Include the affected version, the configuration (store backend, whether
`[server].auth_token` is set, whether `[principals]` is configured, whether
`[identity].require_signatures` is enabled), and steps to reproduce.

## Supported versions

The current release line is 1.8.x. This repository does not define a support
policy for older releases, so please reproduce an issue on the latest release
before reporting it.

## Security model

This summarizes what the repository documents; the authoritative text is
`docs/NCP_PROTOCOL_SPEC.md` (section 5.1) and `docs/NCP_HTTP_API.md`.

- **Loopback by default.** `ncp serve` binds to `127.0.0.1` and requires no
  token on loopback. It refuses to start on a non-loopback
  host unless an auth token is configured. On a loopback bind it rejects
  requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]`
  (DNS rebinding), requests whose `Origin` is neither same-origin nor passed
  with `--cors-origin`, and POSTs that are not `application/json`.
- **One shared bearer token by default.** When `[server].auth_token` is set
  (config file, `NCP_AUTH_TOKEN`, or `--auth-token`), every `/mcp`, `/sse`,
  `/events` and `/api/*` request must present it. By default there is a
  single token for the server, not one per agent. `/healthz` needs no auth.
  Request bodies over 10 MB are rejected, and CORS headers are emitted only
  for origins passed with `--cors-origin`.
- **`agent_id` is self-declared by default; principals are the opt-in fix.**
  Without `[principals]`, the bearer token authenticates the connection, not
  the agent: any caller can act as any `agent_id` (including rewriting
  another agent's `owns`/`must_not`), declare `src=user_verified`, record
  outcomes for any chunk and use any pipeline. Configuring `[principals]`
  (see `docs/NCP_HTTP_API.md`, "Principals") maps each bearer token to one
  principal with a fixed `agent_id`, an optional pipeline allowlist and
  capabilities (`verify`, `record_outcome`, `admin`). The HTTP server then
  rejects identity arguments that differ from the principal, enforces the
  pipeline allowlist, requires `verify` for `src=user_verified` and
  `record_outcome` for outcomes (never crediting the principal's own chunks
  unless it is `admin`), and keys the `ncp_fetch` budget by principal.
  Tokens are stored as SHA-256 hashes or read from an environment variable.
  This is bearer-token identity, not per-message signing, and the stdio
  transport applies no principals.
- **Authorship signing is separate and off by default.** Optional Ed25519
  signing on `ncp_write_memory` and `ncp_emit_whisper` is off by default
  (`[identity].require_signatures = false`). Signing establishes who wrote a
  chunk, not whether the content is true; neither signing nor principals stop
  an agent from writing false content under its own name. The `src` field is
  caller-supplied (apart from the `verify` gate above) unless grounding
  (`[identity].require_grounded_high_trust`) is enabled.
- **Retrieved content is data, not instructions.** Whisper payloads and
  memory chunks written by one agent are injected into other agents' contexts.
  NCP defends the envelope (identity-field mutation, source-tag forgery,
  unbounded payloads, silent write failures) and shows provenance
  (`from:`, `src:`, `trust:`), but it cannot tell a malicious imperative from
  a legitimate one in stored text. The protocol spec requires hosts to tell
  the model to treat such content as information, to verify low-trust or
  `src:agent_inferred` content before acting, and to refuse requests outside
  `owns` or inside `must-not`. `ncp init` writes these instructions into the
  generated turn contract.

Because of this model, without principals anyone who can reach an
unauthenticated or shared-token endpoint can read and write shared memory as
any `agent_id`. Treat that token as a credential for the whole deployment,
configure `[principals]` when agents must not be able to act as each other, and
do not expose the server to untrusted networks.
