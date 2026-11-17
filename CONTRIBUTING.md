# Contributing

Thanks for helping. This tool edits people's financial records, so the bar is safety first.

## Ground rules
- **Never commit or paste real transaction data** (payees, amounts, account names) in code, tests,
  issues or PRs. Use synthetic rows (see `tests/test_engine.py`). Redact before sharing a CSV.
- Safety invariants must keep holding (they are tested): dry-run writes nothing; only income/expense
  rows change; amounts/dates/accounts are never altered; backup before write; verify after write;
  re-runs are idempotent.
- Keep `kb.toml` **generic**: well-known merchants and statement patterns only, no personal payees,
  no personal category names. Personal rules belong in your own untracked `rules.toml`.

## Regional packs
Other cities/states are welcome as `kb_<region>.toml` (public merchants and institutions only).

## Adding knowledge-base rules
- Use word boundaries on short tokens (`\bAMUL\b`, not `AMUL`; it would match inside names).
- Prefer `confidence = "medium"` when a keyword is ambiguous; use `flag = "..."` for transfer-like rows.
- Add a test in `tests/test_engine.py` for any pattern that fixed a false positive.

## Dev setup
    pip install -e '.[dev]'
    pytest

Open an issue before large changes. By contributing you agree your work is licensed under MIT.
