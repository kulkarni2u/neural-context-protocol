# Security

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's private vulnerability
reporting for this repository: open the repository's **Security** tab and
choose **Report a vulnerability**. Please do not open a public issue or pull
request for an unpatched vulnerability.

Include the affected version, the configuration (store backend, whether
`[server].auth_token` is set, whether `[identity].require_signatures` is
enabled), and steps to reproduce.

## Supported versions

The current release line is 1.8.x. This repository does not define a support
policy for older releases, so please reproduce an issue on the latest release
before reporting it.

## Security model

This summarizes what the repository documents; the authoritative text is
`docs/NCP_PROTOCOL_SPEC.md` (section 5.1) and `docs/NCP_HTTP_API.md`.

- **Loopback by default.** `ncp serve` binds to `127.0.0.1` and requires no
  token on loopback. Binding to a non-loopback host without
  `[server].auth_token` logs a warning and leaves the endpoint open.
- **One shared bearer token.** When `[server].auth_token` is set (config
  file, `NCP_AUTH_TOKEN`, or `--auth-token`), every `/mcp`, `/sse` and
  `/events` request must present it. There is a single token for the server,
  not one per agent. `/healthz` needs no auth. Request bodies over 10 MB are
  rejected, and CORS headers are emitted only for origins passed with
  `--cors-origin`.
- **`agent_id` is self-declared.** The bearer token authenticates the
  connection, not the agent. Optional Ed25519 authorship signing on
  `ncp_write_memory` and `ncp_emit_whisper` is off by default
  (`[identity].require_signatures = false`); unless an operator enables it,
  authorship is not authenticated. Signing establishes who wrote a chunk, not
  whether the content is true. The `src` field is also
  caller-supplied unless grounding (`[identity].require_grounded_high_trust`)
  is enabled.
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

Because of this model, anyone who can reach an unauthenticated or shared-token
endpoint can read and write shared memory as any `agent_id`. Treat the token as
a credential for the whole pipeline and do not expose the server to untrusted
networks.
