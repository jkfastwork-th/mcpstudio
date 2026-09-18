# v0.9.7 M6.2.3B hotfix

Fixes repeat certification and durable managed-session port ownership.

- stopped sessions keep their reserved port;
- creating a stopped workspace resumes the same durable session instead of inserting a duplicate;
- certification uses Streamable HTTP-compatible Accept headers for all MCP POSTs.
