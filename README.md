# moneymanager-autocat

[![CI](https://github.com/praveenitdev/moneymanager-autocat/actions/workflows/ci.yml/badge.svg)](https://github.com/praveenitdev/moneymanager-autocat/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Context-aware auto-categoriser for **Realbyte Money Manager** (PC Manager), driven through a
money-manager MCP server such as [money-manager-mcp](https://github.com/praveenitdev/money-manager-mcp).

> **Unofficial.** Not affiliated with or endorsed by Realbyte. It edits your financial records:
> take a Money Manager backup first. Provided as-is under the MIT license (no warranty).
> It has a dry-run default, per-run backup/undo and post-write verification, but you are responsible
> for reviewing the CSVs before applying.

## Quick start
    git clone https://github.com/praveenitdev/moneymanager-autocat && cd moneymanager-autocat
    pip install mcp            # plus: pip install tomli   (Python < 3.11)
    cp rules.example.toml rules.toml      # optional: your own rules (git-ignored)
    python mm_recategorize.py --server-cmd 'node /path/to/money-manager-mcp/<entry>.js'   # dry-run
    # review mm_runs/*.csv, then add --apply

Dry-run by default. Never touches transfers.

## What it does (in order, first hit wins per row)
1. **rules.toml** - your explicit rules (strict: a bad/missing category aborts the run).
2. **kb.toml** - ~75 built-in rules for Indian merchants and bank-statement patterns (Swiggy, Zepto,
   DMart, IRCTC, Apollo, Airtel, card interest/GST, ESOP vesting ...). Checked against *your live
   category tree*: a rule whose category you don't have is skipped, never invented.
3. **Learner** - what you already did. Builds payee -> category from rows you categorised yourself.
   High confidence = same payee, >=3 past rows, >=90% agree. Otherwise medium.
4. **Refund pairing** - an income credit that mirrors an earlier expense at the same payee
   (<=90 days, amount <= expense) -> Refunds > Shopping / Orders & Services / Travel.
5. **Flags** - never changed, listed for you: card bill payments, EMI principal/foreclosure,
   ATM cash, gold/investment purchases, "untracked"/opening-balance placeholders, cash deposits,
   "payee unknown". They should be *transfers*, not expenses/income.
6. **Audit** - payees you categorise one way >=80% of the time (>=5 rows) with a few rows elsewhere
   (e.g. 21 "Card interest/GST: IGST" rows in Bank Charges vs 95 in Loans > Interest).

## Confidence gates
| level  | what                                   | applied by         |
|--------|----------------------------------------|--------------------|
| high   | your rules, strong kb, strong learner  | `--apply`          |
| medium | weak kb, weak learner, refund pairs, amounts > `--auto-limit` (5,000) for kb/learned | review CSV |
| flag   | looks like a transfer                  | never              |

## Run
    pip install mcp            # + tomli if Python < 3.11
    python mm_recategorize.py --server-cmd 'node /path/to/money-manager-mcp/<entry>.js'          # dry-run
    python mm_recategorize.py --server-cmd '...' --apply                                          # high only
    python mm_recategorize.py --server-cmd '...' --apply --min-conf medium --yes                  # also medium
    python mm_recategorize.py --server-cmd '...' --apply --apply-csv mm_runs/mm_review_X.csv      # reviewed rows
    python mm_recategorize.py --server-cmd '...' --undo mm_runs/mm_backup_X.json

Each run writes to `mm_runs/`: `mm_apply_*.csv` (what --apply changes), `mm_review_*.csv` (medium),
`mm_inconsistent_*.csv` (audit), `mm_flagged_*.csv` (transfer-like), plus `mm_backup_*.json` on apply.

**Review workflow:** open a review/inconsistent CSV, put `y` in `approve` (you may edit
`suggested_category` / `suggested_sub`), save, then `--apply --apply-csv FILE`. Rows that changed
since the CSV was made are skipped. Approved rows may move out of a non-empty category.

## Other flags
`--why TEXT` explain the decision for every row containing TEXT (which rule/learner, why)
`--unmatched N` top unclassified payees | `--only-rule NAME` | `--from/--to` | `--no-kb` | `--no-learn`
`--auto-limit 5000` | `--max-changes 800` (brake; `--yes` to exceed) | `--max-errors 3` | `--connect HOST:PORT`
Offline test (no server): `--dump-json all.json --init-json init.json`.

## Safety
Only income/expense rows. Default: only Other/blank rows, or rows missing a subcategory.
Backup first; after writing it re-fetches and verifies rows landed, monthly totals unchanged, every
account balance unchanged; any mismatch prints the `--undo` command. Re-runs are idempotent.

## Extending
`rules.toml` / `kb.toml` fields: name, type, match (regex, case-insens.), exclude, pay_type,
not_pay_type, match_detail, min_amount/max_amount, date_from/date_to, category, subcategory,
confidence (high|medium), override, also_from, or `flag = "reason"` instead of a category.
Tip: after you categorise a payee once in the app, the learner picks it up on the next run.

## Known limits
- Not run against your live server (tested against an in-memory fake + your real history offline).
- UPI to people (~660 rows) is not guessable; only your own history can label them.
- Never converts expenses into transfers; use the flagged CSV as your to-do list.
- Short kb tokens can still misfire on person names; that's why amounts > 5,000 go to review.

## Tests, contributing, license
`pip install -e '.[dev]' && pytest` (synthetic data only). See [CONTRIBUTING.md](CONTRIBUTING.md),
[SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md). Licensed under [MIT](LICENSE).
