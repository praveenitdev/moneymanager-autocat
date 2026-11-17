# Changelog

## 0.2.0
- Context-aware engine: your rules, a merchant knowledge base (`kb.toml`), a learner that copies your
  own past categorisation, refund pairing, transfer-like flags and an audit of inconsistent rows.
- Confidence gates, review CSV workflow (`--apply-csv`), `--why`, `--auto-limit`, `--max-changes`.
- Handles duplicate category names, emoji/trailing-space names, stale CSVs.

## 0.1.0
- Rule-based dry-run/apply/undo recategoriser with post-apply verification.
