# Security

## Reporting a vulnerability

Please email **harrywinter06@gmail.com** rather than opening a public issue.

## Threat model — what agentlog is and isn't

agentlog is a **local-first developer tool**. The threat model assumes:

- It runs on a developer's machine, alongside the code under instrumentation.
- The SQLite store at `~/.agentlog/traces.db` (or `$AGENTLOG_DB`) contains whatever inputs/outputs you pass to instrumented functions.
- The dashboard listens on `127.0.0.1:7777` by default and has **no authentication**. Don't bind it to a public interface.

## What this means in practice

- **Don't run the dashboard on a multi-user host** without an authenticating reverse proxy in front.
- **Don't trace functions that handle secrets without filtering** — agentlog will faithfully record them. `AGENTLOG_MAX_PAYLOAD_BYTES` truncates large I/O but does not redact.
- **The Anthropic/OpenAI wrappers strip API keys from logged kwargs** (only known safe keys like `model`, `max_tokens`, `temperature`, `system`, `tools` are recorded). If you set a non-standard kwarg, it is not logged.
- **JSON export files contain everything in the DB.** Treat exported `.jsonl` files as sensitive.
- **OpenTelemetry-ingested spans are stored verbatim** — agentlog does not filter attributes.

## What's intentionally out of scope (for now)

- Encrypted-at-rest storage
- Per-user / per-team access control
- Audit logging of dashboard access
- PII redaction

If you need any of these, you probably want a hosted observability tool (Langfuse Cloud, LangSmith, Helicone) instead of agentlog.
