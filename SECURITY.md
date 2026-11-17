# Security and privacy

- The tool talks only to the MCP server **you** start locally; it makes no network calls of its own.
- `mm_runs/` (backups and CSVs) contains your real transactions. It is git-ignored; never publish it.
- Your personal `rules.toml` is git-ignored for the same reason; share only `rules.example.toml`.
- Found a vulnerability or a data-loss bug? Please open a private security advisory on GitHub
  (Security tab) instead of a public issue, and redact any financial data.
