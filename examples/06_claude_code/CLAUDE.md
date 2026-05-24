# Example Claude Code Conventions

- Start each turn by calling `ncp_get_context`.
- End each turn by writing durable memory with `ncp_write_memory`.
- Use `ncp_fetch` only when the active turn needs bounded retrieval.
- Prefer recent refs and whispers over replaying full chat history.
