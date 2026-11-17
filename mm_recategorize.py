#!/usr/bin/env python3
"""
mm_recategorize.py - safe, context-aware recategoriser for Realbyte Money Manager.

Talks to your own money-manager-mcp server over MCP/stdio (reuses your verified connection).

HOW IT DECIDES (strongest first; first hit wins per row)
  1. rules.toml   - your explicit rules                              (confidence: high)
  2. kb.toml      - built-in merchant/keyword knowledge base         (high / medium)
     Both are checked against YOUR live category tree: a rule whose category does not exist
     is skipped (kb) or aborts the run (rules.toml), so it can never invent categories.
  3. learned      - what you already did: the payee's past rows in categories you chose
                    (high only when >=3 past rows and >=90% agree; else medium)
  4. refund pair  - an income row that mirrors an earlier expense at the same payee
  5. flags        - rows that look like transfers (card bill, EMI principal, ATM, gold, ...):
                    never changed, listed for you to fix by hand (they should be transfers)

SAFETY
  * DRY-RUN by default. --apply writes only rows at/above --min-conf (default: high).
  * Medium suggestions go to a review CSV: put y in the `approve` column (or edit the
    suggested category/subcategory), then run  --apply-csv FILE  to apply just those.
  * Only income/expense rows are touched; transfers/balance rows never.
  * Only uncategorised rows (Other/blank) or "right category, missing subcategory" rows are
    changed unless a rule says override=true / also_from=[...].
  * Amounts, dates, accounts, notes are re-sent unchanged; full IDs come from fetched data.
  * Backup before writing (--undo FILE restores). After writing: re-fetch and verify rows,
    monthly totals and every account balance.  Idempotent: re-runs skip finished rows.
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import datetime as dt
import json
import os
import re
import shlex
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # pip install tomli

INCOME, EXPENSE = "0", "1"
TYPE_OF = {EXPENSE: "expense", INCOME: "income"}
FULL_ID = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
CONF_RANK = {"flag": 0, "medium": 1, "high": 2}
HERE = Path(__file__).resolve().parent


def norm(s: str) -> str:
    """Lowercase, drop emoji/punctuation (keeps & and digits), collapse spaces."""
    s = re.sub(r"[^0-9A-Za-z&]+", " ", s or "")
    return re.sub(r"\s+", " ", s).strip().casefold()


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def is_uncat(row: dict) -> bool:
    return norm(row["mbCategory"]) in ("", "other") and not norm(row["subCategory"])


# --------------------------------------------------------------------------- categories
class Categories:
    """Resolves (type, category, subcategory) -> ids/names Money Manager wants.

    Handles: emoji/trailing-space/case differences, duplicate category names (e.g. income
    '📈 Trading' and 'Trading' - the one that actually has the subcategory wins), and
    categories that exist only as names."""

    def __init__(self, init: dict):
        self.by_type: dict[str, dict[str, list]] = {}
        self.names: dict[str, list[str]] = {}
        for typ in ("expense", "income"):
            cats: dict[str, list] = defaultdict(list)
            for c in init["categories"][typ]:
                subs = {}
                for s in c.get("mcsc", []):
                    subs.setdefault(norm(s["mcscname"]), s)
                cats[norm(c["mcname"])].append({"c": c, "subs": subs})
            self.by_type[typ] = cats
            self.names[typ] = [c["mcname"] for c in init["categories"][typ]]
            for k, v in cats.items():
                if len(v) > 1:
                    warn(f"{typ}: {len(v)} categories normalise to '{k}' "
                         f"({', '.join(e['c']['mcname'] for e in v)}); using whichever has the "
                         f"needed subcategory, else the first. Consider merging them in the app.")

    def exists(self, typ: str, cat: str, sub: str = "") -> bool:
        try:
            self.resolve(typ, cat, sub)
            return True
        except KeyError:
            return False

    def resolve(self, typ: str, cat: str, sub: str = "") -> dict:
        ents = self.by_type[typ].get(norm(cat))
        if not ents:
            raise KeyError(f"{typ} category '{cat}' does not exist in Money Manager")
        if sub:
            for ent in ents:
                s = ent["subs"].get(norm(sub))
                if s:
                    c = ent["c"]
                    return dict(mcid=c["mcid"], mbCategory=c["mcname"],
                                mcscid=s["mcscid"], subCategory=s["mcscname"])
            raise KeyError(f"{typ} subcategory '{cat} > {sub}' does not exist")
        c = ents[0]["c"]
        return dict(mcid=c["mcid"], mbCategory=c["mcname"], mcscid=c["mcid"], subCategory="")

    def by_ids(self, typ: str, mcid: str, mcscid: str) -> dict | None:
        for ents in self.by_type[typ].values():
            for ent in ents:
                if ent["c"]["mcid"] == mcid:
                    if mcscid and mcscid != mcid:
                        for s in ent["subs"].values():
                            if s["mcscid"] == mcscid:
                                return dict(mcid=mcid, mbCategory=ent["c"]["mcname"],
                                            mcscid=mcscid, subCategory=s["mcscname"])
                    return dict(mcid=mcid, mbCategory=ent["c"]["mcname"], mcscid=mcid,
                                subCategory="")
        return None


def label(t: dict) -> str:
    return f"{t['mbCategory'].strip()} > {t['subCategory'].strip()}" if t["subCategory"] \
        else t["mbCategory"].strip()


# --------------------------------------------------------------------------- rules
class Rule:
    """One matching rule (rules.toml / kb.toml). A rule with `flag = "reason"` and no category
    never changes anything: it marks rows that should be handled by hand (e.g. transfers)."""

    def __init__(self, d: dict, idx: int, source: str):
        self.idx, self.source = idx, source
        self.name = d.get("name") or f"{source}#{idx}"
        self.type = d.get("type", "expense")
        if self.type not in ("expense", "income"):
            raise ValueError(f"{self.name}: type must be expense|income")
        fl = re.IGNORECASE
        self.match = re.compile(d["match"], fl)
        self.exclude = re.compile(d["exclude"], fl) if d.get("exclude") else None
        self.pay_type = re.compile(d["pay_type"], fl) if d.get("pay_type") else None
        self.not_pay_type = re.compile(d["not_pay_type"], fl) if d.get("not_pay_type") else None
        self.match_detail = bool(d.get("match_detail", False))
        self.min_amount, self.max_amount = d.get("min_amount"), d.get("max_amount")
        self.date_from, self.date_to = d.get("date_from"), d.get("date_to")
        self.flag = d.get("flag")
        self.category = d.get("category")
        self.subcategory = d.get("subcategory", "")
        if not self.flag and not self.category:
            raise ValueError(f"{self.name}: needs `category` or `flag`")
        self.conf = d.get("confidence", "high")
        if self.conf not in ("high", "medium"):
            raise ValueError(f"{self.name}: confidence must be high|medium")
        self.override = bool(d.get("override", False))
        self.also_from = {norm(x) for x in d.get("also_from", [])}
        self.target = None
        self.hits = 0

    def matches(self, row: dict) -> bool:
        if TYPE_OF.get(row["inOutCode"]) != self.type:
            return False
        text = row["mbContent"] or ""
        if self.match_detail:
            text += " " + (row.get("mbDetailContent") or "")
        if not self.match.search(text):
            return False
        if self.exclude and self.exclude.search(text):
            return False
        pt = row.get("payType") or ""
        if self.pay_type and not self.pay_type.search(pt):
            return False
        if self.not_pay_type and self.not_pay_type.search(pt):
            return False
        amt = float(row["mbCash"])
        if self.min_amount is not None and amt < self.min_amount:
            return False
        if self.max_amount is not None and amt > self.max_amount:
            return False
        if self.date_from and row["mbDate"] < self.date_from:
            return False
        if self.date_to and row["mbDate"] > self.date_to:
            return False
        return True


def eligible(row: dict, target: dict, override=False, also_from=frozenset()) -> bool:
    """May this row be moved to `target`?  (see SAFETY in the module docstring)"""
    cur_cat, cur_sub = norm(row["mbCategory"]), norm(row["subCategory"])
    if override or is_uncat(row):
        return True
    if cur_cat == norm(target["mbCategory"]) and not cur_sub and target["subCategory"]:
        return True                                  # right category, add the subcategory
    return cur_cat in also_from


def load_rules(path: Path, cats: Categories, source: str, strict: bool) -> list[Rule]:
    """strict (your rules.toml): a bad rule aborts.  Not strict (kb.toml): rules whose
    categories you don't have are skipped, so the KB adapts to *your* category tree."""
    if not path.exists():
        if source == "kb" or path.name == "rules.toml":
            print(f"  (no {path.name} found - continuing without it"
                  f"{'; copy rules.example.toml to rules.toml to add your own rules' if strict else ''})")
            return []
        sys.exit(f"rules file not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rules, problems, skipped = [], [], 0
    for i, d in enumerate(data.get("rule", []), 1):
        try:
            r = Rule(d, i, source)
            if r.category:
                r.target = cats.resolve(r.type, r.category, r.subcategory)
            rules.append(r)
        except KeyError as e:
            if strict:
                problems.append(f"rule #{i} ({d.get('name', '?')}): {e}")
            else:
                skipped += 1
        except (re.error, ValueError) as e:
            problems.append(f"{source} rule #{i} ({d.get('name', '?')}): {e}")
    if problems:
        print("Rule problems (fix the file, or create the missing categories in the app):")
        for p in problems:
            print("   -", p)
        sys.exit(2)
    if skipped:
        print(f"  ({path.name}: skipped {skipped} rules whose categories you don't have)")
    return rules


# --------------------------------------------------------------------------- learner
GENERIC_KEYS = re.compile(r"^(UPI|UPI OUT PAYEE UNKNOWN|EMI PRINCIPAL.*|PAYMENT.*|TRANSFER.*|"
                          r"NEFT.*|IMPS.*|MOBFT.*|CASH.*|ATM.*|OTHER|UNKNOWN|.{0,3})$")


def tokens_key(s: str) -> str:
    """Looser key: first two alphabetic words (so 'ZOMATO NEW DELHI' ~ 'ZOMATO GURGAON')."""
    w = [t for t in re.findall(r"[A-Z]{3,}", payee_key(s)) if t not in ("UPI", "IN", "PVT", "LTD",
                                                                          "PRIVATE", "LIMITED")]
    return " ".join(w[:2])


class Learner:
    """Learns payee -> category from rows the user already categorised (not Other/blank)."""

    def __init__(self, rows: list[dict]):
        self.exact: dict[tuple, Counter] = defaultdict(Counter)
        self.loose: dict[tuple, Counter] = defaultdict(Counter)
        for r in rows:
            if r["inOutCode"] not in (INCOME, EXPENSE) or is_uncat(r):
                continue
            typ = TYPE_OF[r["inOutCode"]]
            tgt = (norm(r["mbCategory"]), norm(r["subCategory"]))
            ek, lk = payee_key(r["mbContent"]), tokens_key(r["mbContent"])
            if ek and not GENERIC_KEYS.match(ek):
                self.exact[(typ, ek)][tgt] += 1
            if len(lk) >= 5:
                self.loose[(typ, lk)][tgt] += 1

    def suggest(self, row: dict):
        """-> (target_ids, confidence, why) or None"""
        typ = TYPE_OF[row["inOutCode"]]
        ek, lk = payee_key(row["mbContent"]), tokens_key(row["mbContent"])
        for tbl, key, kind in ((self.exact, ek, "same payee"), (self.loose, lk, "similar payee")):
            c = tbl.get((typ, key))
            if not c:
                continue
            (tgt, n), total = c.most_common(1)[0], sum(c.values())
            share = n / total
            if share >= 0.9 and total >= 3 and kind == "same payee":
                conf = "high"
            elif share >= 0.7 and total >= 2 or (share == 1 and total >= 1):
                conf = "medium"
            else:
                continue
            return tgt, conf, f"{kind}: {n}/{total} past rows in this category"
        return None


# --------------------------------------------------------------------------- planning
class Decision:
    """Why a row is (or isn't) being changed."""
    __slots__ = ("name", "source", "conf", "reason")

    def __init__(self, name, source, conf, reason=""):
        self.name, self.source, self.conf, self.reason = name, source, conf, reason


class Plan:
    def __init__(self):
        self.changes = []          # (Decision, row, target)  - proposed moves
        self.flagged = []          # (Decision, row)          - look like transfers etc.
        self.protected = Counter()  # matched a rule but row already categorised elsewhere
        self.already = 0
        self.conflicts = 0         # rows matched by >1 rule with different targets
        self.rules: list[Rule] = []
        self.person_like = []      # uncategorised UPI-to-a-person rows nothing could classify
        self.unresolved = []       # everything else still uncategorised
        self.inconsistent = []     # (Decision, row, target): minority rows of a payee that is
                                   # ~always categorised another way (likely slips)

    def at(self, min_conf: str):
        k = CONF_RANK[min_conf]
        return [c for c in self.changes if CONF_RANK[c[0].conf] >= k]


PERSON = re.compile(r"^(?:UPI(?: IN)?\s*:\s*)?([A-Z][A-Z .]{2,30})$", re.I)
MERCHANT_WORDS = re.compile(r"PVT|LTD|LIMITED|STORE|MART|SHOP|TRADERS|ENTERPRISE|AGENCIES|HOTEL|"
                            r"RESTAU|CAFE|BAKERY|MEDICAL|PHARMA|CLINIC|HOSPITAL|BANK|FINANCE|"
                            r"TECHNOLOG|SERVICES|RETAIL|WEDDING|TEMPLE|FOODS|SWEETS|WINES|BAR\b",
                            re.I)


def looks_like_person(text: str) -> bool:
    t = re.sub(r"\[\d+\]|\d+", "", text or "").strip()
    return bool(PERSON.match(t)) and not MERCHANT_WORDS.search(t) and len(t.split()) <= 3


def refund_pairs(rows: list[dict], cats: Categories):
    """Income rows (uncategorised) that mirror an earlier expense at the same payee within
    90 days with amount <= expense  ->  Refunds > Shopping / Orders & Services / Travel."""
    exp: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["inOutCode"] == EXPENSE:
            exp[tokens_key(r["mbContent"])].append(r)
    out = {}
    for r in rows:
        if r["inOutCode"] != INCOME or not is_uncat(r):
            continue
        key = tokens_key(r["mbContent"])
        if len(key) < 5 or not re.search(r"CREDIT|REFUND|REVERS|RETURN", r["mbContent"], re.I) \
                and "refund" not in (r.get("mbDetailContent") or "").lower():
            continue
        d1 = dt.date.fromisoformat(r["mbDate"])
        cand = [e for e in exp.get(key, [])
                if 0 <= (d1 - dt.date.fromisoformat(e["mbDate"])).days <= 90
                and float(e["mbCash"]) >= float(r["mbCash"]) * 0.99]
        if not cand:
            continue
        e = max(cand, key=lambda x: x["mbDate"])
        ecat = norm(e["mbCategory"])
        sub = ("Travel" if ecat.startswith("travel") else
               "Orders & Services" if ecat in ("food", "groceries", "transport", "home services")
               else "Shopping")
        pick = next((x for x in (sub, "Shopping", "Other", "") if cats.exists("income", "Refunds", x)),
                    None)
        if pick is not None:
            out[r["id"]] = (cats.resolve("income", "Refunds", pick),
                            f"mirrors {e['mbDate']} expense {float(e['mbCash']):,.0f} "
                            f"at same payee ({label(dict(mbCategory=e['mbCategory'], subCategory=e['subCategory']))})")
    return out


def audit(rows, learner: Learner, cats: Categories):
    """Payees you categorise one way >= 80% of the time (and >= 5 rows) but a few rows went
    elsewhere: probably slips. Suggested fix = the majority category (needs your approval)."""
    out = []
    for r in rows:
        if r["inOutCode"] not in (INCOME, EXPENSE) or is_uncat(r):
            continue
        typ = TYPE_OF[r["inOutCode"]]
        ek = payee_key(r["mbContent"])
        c = learner.exact.get((typ, ek))
        if not c or sum(c.values()) < 5:
            continue
        (cat, sub), n = c.most_common(1)[0]
        if n / sum(c.values()) < 0.8 or (cat, sub) == (norm(r["mbCategory"]), norm(r["subCategory"])):
            continue
        if cat in ("", "other"):
            continue
        try:
            t = cats.resolve(typ, cat, sub)
        except KeyError:
            continue
        out.append((Decision("inconsistent", "audit", "medium",
                             f"{n}/{sum(c.values())} rows of this payee are {label(t)}"), r, t))
    return out


def brake(d: Decision, row: dict, limit: float) -> Decision:
    """Guessed (kb / learned) high-confidence picks above `limit` rupees need a human look.
    Your own rules.toml rules are exempt - you wrote them on purpose."""
    if d.source in ("kb", "learned") and d.conf == "high" and float(row["mbCash"]) > limit:
        d.conf, d.reason = "medium", (d.reason + "; " if d.reason else "") + f"amount > {limit:,.0f}"
    return d


def plan_all(rows: list[dict], rules: list[Rule], cats: Categories, learn: bool = True,
             auto_limit: float = 5000) -> Plan:
    plan = Plan()
    plan.rules = rules
    learner = Learner(rows) if learn else None
    pairs = refund_pairs(rows, cats) if learn else {}
    for row in rows:
        if row["inOutCode"] not in (INCOME, EXPENSE):
            continue
        if not FULL_ID.match(row["id"]):
            raise RuntimeError(f"unexpected id format: {row['id']!r}")
        hit = [r for r in rules if r.matches(row)]
        for r in hit:
            r.hits += 1
        if len({(x.target["mcid"], x.target["mcscid"]) for x in hit if x.target}) > 1:
            plan.conflicts += 1
        first = hit[0] if hit else None
        if first and first.flag:
            if is_uncat(row):
                plan.flagged.append((Decision(first.name, first.source, "flag", first.flag), row))
            continue
        if first:
            t = first.target
            if row["mcid"] == t["mcid"] and norm(row["subCategory"]) == norm(t["subCategory"]):
                plan.already += 1
            elif eligible(row, t, first.override, first.also_from):
                plan.changes.append((brake(Decision(first.name, first.source, first.conf), row,
                                           auto_limit), row, t))
            else:
                plan.protected[first.name] += 1
            continue
        if not is_uncat(row):
            continue
        # ---- nothing in rules/kb: learn from history, then refund pairing
        got = None
        if learner:
            s = learner.suggest(row)
            if s:
                (cname, sname), conf, why = s
                try:
                    t = cats.resolve(TYPE_OF[row["inOutCode"]], cname, sname)
                except KeyError:           # that category no longer exists
                    t = None
                if t and not (norm(t["mbCategory"]) == "other" and not t["subCategory"]):
                    got = (Decision("learned: " + label(t), "learned", conf, why), row, t)
        if not got and row["id"] in pairs:
            t, why = pairs[row["id"]]
            got = (Decision("refund pair: " + label(t), "pair", "medium", why), row, t)
        if got:
            brake(got[0], row, auto_limit)
            plan.changes.append(got)
        elif row["inOutCode"] == EXPENSE and looks_like_person(row["mbContent"]):
            plan.person_like.append(row)
        else:
            plan.unresolved.append(row)
    if learner:
        plan.inconsistent = audit(rows, learner, cats)
    return plan


# --------------------------------------------------------------------------- MCP transport
class MM:
    """Thin client over your money-manager-mcp server."""

    def __init__(self, session, mbid: str, timeout: int):
        self.s, self.mbid, self.timeout = session, mbid, timeout

    async def call(self, tool: str, args: dict, timeout: int | None = None):
        last = None
        for attempt in range(3):
            try:
                res = await asyncio.wait_for(self.s.call_tool(tool, args), timeout or self.timeout)
                text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                if res.isError:
                    raise RuntimeError(text or "tool error")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
            except asyncio.TimeoutError:
                raise
            except Exception as e:  # transient: back off and retry
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{tool} failed after retries: {last}")

    async def init(self):
        return await self.call("init_get_data", {"mbid": self.mbid})

    async def assets(self) -> dict[str, float]:
        d = await self.call("asset_list", {})
        out = {}
        for g in d["assetGroups"]:
            for a in g.get("children", []):
                out[a["assetId"]] = round(float(a["assetMoney"]), 2)
        return out

    async def month(self, y: int, m: int, timeouts: list):
        start = dt.date(y, m, 1)
        end = dt.date(y, m, calendar.monthrange(y, m)[1])
        try:
            d = await self.call("transaction_list", {"startDate": str(start), "endDate": str(end),
                                                     "mbid": self.mbid})
        except asyncio.TimeoutError:
            # PC Manager can hang on months with no rows; treat as empty but report it.
            timeouts.append(f"{y}-{m:02d}")
            return []
        return d["transactions"] if isinstance(d, dict) else []

    async def fetch(self, d_from: dt.date, d_to: dt.date):
        timeouts, rows = [], {}
        y, m = d_from.year, d_from.month
        while (y, m) <= (d_to.year, d_to.month):
            for r in await self.month(y, m, timeouts):
                rows[r["id"]] = r
            m += 1
            if m == 13:
                y, m = y + 1, 1
        return list(rows.values()), timeouts

    async def update(self, row: dict, t: dict):
        payload = dict(id=row["id"], mbDate=row["mbDate"], assetId=row["assetId"],
                       payType=row["payType"], mcid=t["mcid"], mbCategory=t["mbCategory"],
                       mcscid=t["mcscid"], subCategory=t["subCategory"], mbCash=row["mbCash"],
                       inOutCode=row["inOutCode"], inOutType=row["inOutType"],
                       mbContent=row["mbContent"], mbDetailContent=row.get("mbDetailContent", ""))
        res = await self.call("transaction_update", payload)
        if not (isinstance(res, dict) and res.get("success")):
            raise RuntimeError(f"update not confirmed for {row['id']}: {res}")


def totals(rows):
    t = defaultdict(float)
    for r in rows:
        if r["inOutCode"] in (INCOME, EXPENSE):
            t[(r["mbDate"][:7], r["inOutCode"])] += float(r["mbCash"])
    return {k: round(v, 2) for k, v in t.items()}


def payee_key(s: str) -> str:
    s = re.sub(r"^(UPI IN|UPI|CREDIT|OUT|IN)\s*:\s*", "", s or "", flags=re.I)
    s = re.sub(r"UPI[-\s]?\d+", "", s, flags=re.I)
    s = re.sub(r"\[\d+\]|\d{4,}", "", s)
    return re.sub(r"\s+", " ", s).strip().upper()[:40]



# --------------------------------------------------------------------------- reporting
def money(x): return f"{x:>12,.2f}"


def write_csv(path: Path, header, rows_):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows_)


def show(plan: Plan, rows, args, outdir: Path, stamp: str, min_conf: str):
    ie = [r for r in rows if r["inOutCode"] in (INCOME, EXPENSE)]
    unc = [r for r in ie if is_uncat(r)]
    act = plan.at(min_conf)
    rev = [c for c in plan.changes if CONF_RANK[c[0].conf] < CONF_RANK[min_conf]]
    print(f"\nScanned {len(rows)} rows ({len(ie)} income/expense, {len(unc)} uncategorised)")
    print(f"  will change now ({min_conf}+) : {len(act)}")
    print(f"  needs your review (medium)  : {len(rev)}   -> review CSV")
    print(f"  flagged (look like transfers): {len(plan.flagged)}")
    print(f"  already correct             : {plan.already}")
    print(f"  still unclassified          : {len(plan.unresolved) + len(plan.person_like)}"
          f"  ({len(plan.person_like)} are UPI to a person)")
    after = len(unc) - len(act)
    print(f"  uncategorised after --apply : {after}   (after approving review rows: "
          f"{after - len(rev)})")

    def group(items, title):
        by, amt = Counter(), defaultdict(float)
        for d, row, t in items:
            by[d.name] += 1
            amt[d.name] += float(row["mbCash"])
        if by:
            print(f"\n{title}")
            for name, n in by.most_common(args.top_rules):
                print(f"   {n:4d} {money(amt[name])}  {name}")
            if len(by) > args.top_rules:
                print(f"   ... {len(by) - args.top_rules} more groups (see CSV)")
    group(act, f"Applying ({min_conf}+):")
    group(rev, "Review (medium confidence):")

    if plan.protected:
        print("\nMatched a rule but NOT moved (already categorised elsewhere; "
              "use override=true / also_from to move):")
        for name, n in plan.protected.most_common(12):
            print(f"   {n:4d}  {name}")
    if plan.conflicts:
        print(f"\n{plan.conflicts} rows matched several rules pointing at different categories "
              f"(first rule won; user rules.toml always beats kb.toml).")
    dead = [r.name for r in plan.rules if r.hits == 0 and r.source == "user"]
    if dead:
        print(f"\nYour rules that matched nothing in this date range ({len(dead)}): "
              + "; ".join(dead[:10]) + (" ..." if len(dead) > 10 else ""))

    if plan.inconsistent:
        by = Counter(d.reason for d, _, _ in plan.inconsistent)
        print(f"\nInconsistent: {len(plan.inconsistent)} rows sit outside the category you use for "
              f"the same payee ~always (mm_inconsistent CSV, needs your approval):")
        for k, n in by.most_common(8):
            print(f"   {n:4d}  {k}")
    if plan.flagged:
        by, amt = Counter(), defaultdict(float)
        for d, row in plan.flagged:
            by[d.reason] += 1
            amt[d.reason] += float(row["mbCash"])
        print("\nFlagged - these look like TRANSFERS/non-expenses; fix by hand "
              "(never auto-changed):")
        for k, n in by.most_common():
            print(f"   {n:4d} {money(amt[k])}  {k}")

    # ---- files
    hdr = ["id", "date", "type", "amount", "account", "payee", "current", "suggested_category",
           "suggested_sub", "confidence", "source", "why", "approve"]

    def line(d, row, t, approve):
        return [row["id"], row["mbDate"], TYPE_OF[row["inOutCode"]], row["mbCash"], row["payType"],
                row["mbContent"], label(dict(mbCategory=row["mbCategory"],
                                             subCategory=row["subCategory"])),
                t["mbCategory"].strip(), t["subCategory"].strip(), d.conf, d.source,
                d.reason or d.name, approve]
    write_csv(outdir / f"mm_apply_{stamp}.csv", hdr, [line(d, r, t, "auto") for d, r, t in act])
    write_csv(outdir / f"mm_review_{stamp}.csv", hdr, [line(d, r, t, "") for d, r, t in rev])
    write_csv(outdir / f"mm_inconsistent_{stamp}.csv", hdr,
              [line(d, r, t, "") for d, r, t in plan.inconsistent])
    write_csv(outdir / f"mm_flagged_{stamp}.csv",
              ["id", "date", "type", "amount", "account", "payee", "why"],
              [[r["id"], r["mbDate"], TYPE_OF[r["inOutCode"]], r["mbCash"], r["payType"],
                r["mbContent"], d.reason] for d, r in plan.flagged])
    print(f"\nFiles in {outdir}/:  mm_apply_{stamp}.csv   mm_review_{stamp}.csv   "
          f"mm_flagged_{stamp}.csv  mm_inconsistent_{stamp}.csv")

    if args.unmatched:
        c, amt = Counter(), defaultdict(float)
        for r in plan.unresolved + plan.person_like:
            k = (TYPE_OF[r["inOutCode"]], payee_key(r["mbContent"]))
            c[k] += 1
            amt[k] += float(r["mbCash"])
        print(f"\nTop {args.unmatched} still-unclassified payees (add a rule, or categorise once "
              f"in the app and the learner will pick it up):")
        for k, _ in sorted(amt.items(), key=lambda kv: -kv[1])[:args.unmatched]:
            print(f"   {k[0]:7s} {c[k]:4d} x {money(amt[k])}  {k[1]}")


def why(plan_rows, rules, cats, text: str):
    """--why TEXT: explain what the engine would do for every row whose title contains TEXT."""
    plan = plan_all(plan_rows, rules, cats)
    ids = {r["id"] for r in plan_rows if text.lower() in (r["mbContent"] or "").lower()}
    print(f"\n{len(ids)} rows contain {text!r}")
    ch = {row["id"]: (d, t) for d, row, t in plan.changes}
    fl = {row["id"]: d for d, row in plan.flagged}
    for r in plan_rows:
        if r["id"] not in ids:
            continue
        cur = label(dict(mbCategory=r["mbCategory"], subCategory=r["subCategory"]))
        if r["id"] in ch:
            d, t = ch[r["id"]]
            out = f"-> {label(t)}  [{d.conf}; {d.source}: {d.reason or d.name}]"
        elif r["id"] in fl:
            out = f"FLAGGED: {fl[r['id']].reason}"
        else:
            out = "no change"
        print(f"  {r['mbDate']} {float(r['mbCash']):>10,.2f}  {r['mbContent'][:34]:34s} "
              f"[{cur}] {out}")


# --------------------------------------------------------------------------- main flow
async def run(args):
    offline = args.dump_json is not None
    d_from = dt.date.fromisoformat(args.date_from)
    d_to = dt.date.fromisoformat(args.date_to) if args.date_to else dt.date.today()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if offline:  # read-only test mode against saved JSON
        init = json.loads(Path(args.init_json).read_text())
        raw = json.loads(Path(args.dump_json).read_text())
        rows = raw["transactions"] if isinstance(raw, dict) else raw
        rows = [r for r in rows if d_from.isoformat() <= r["mbDate"] <= d_to.isoformat()]
        cats = Categories(init)
        rules = load_all(args, cats)
        if args.why:
            why(rows, rules, cats, args.why)
        else:
            show(plan_all(rows, rules, cats, not args.no_learn, args.auto_limit), rows, args, outdir, stamp,
                 args.min_conf)
        print("\n(offline mode: nothing was sent to Money Manager)")
        return

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd = shlex.split(args.server_cmd or os.environ.get("MM_SERVER_CMD", ""))
    if not cmd:
        sys.exit("Give your server command via --server-cmd or MM_SERVER_CMD, e.g.\n"
                 "  --server-cmd 'node /Users/you/.../money-manager-mcp/dist/index.js'")
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=dict(os.environ))

    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as session:
            await session.initialize()
            mm = MM(session, args.mbid, args.timeout)
            if args.connect:
                host, _, port = args.connect.partition(":")
                await mm.call("connection_set", {"host": host, "port": int(port)})
            await core(mm, args, d_from, d_to, outdir, stamp)


def load_all(args, cats: Categories) -> list[Rule]:
    user = load_rules(Path(args.rules), cats, "user", strict=True)
    kb = [] if args.no_kb else load_rules(Path(args.kb), cats, "kb", strict=False)
    rules = user + kb
    print(f"Loaded {len(user)} user rules + {len(kb)} knowledge-base rules")
    return rules


def changes_from_csv(path: Path, rows, cats: Categories):
    """Rows the user approved (approve = y/yes/1/x) in a review CSV; suggested_category /
    suggested_sub may have been edited by hand. Rows must still be in the category the CSV recorded (stale CSVs are skipped)."""
    by_id = {r["id"]: r for r in rows}
    out, bad = [], []
    with path.open(newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            if (rec.get("approve") or "").strip().lower() not in ("y", "yes", "1", "x", "true"):
                continue
            row = by_id.get(rec["id"])
            if not row:
                bad.append(f"{rec['id'][:8]} not in fetched range")
                continue
            typ = TYPE_OF[row["inOutCode"]]
            try:
                t = cats.resolve(typ, rec["suggested_category"], rec["suggested_sub"].strip())
            except KeyError as e:
                bad.append(f"{rec['id'][:8]} {e}")
                continue
            cur = label(dict(mbCategory=row["mbCategory"], subCategory=row["subCategory"]))
            if cur != rec.get("current", cur):
                bad.append(f"{rec['id'][:8]} changed since the CSV was made "
                           f"(now '{cur}', CSV said '{rec['current']}') - skipped")
                continue
            out.append((Decision("reviewed", "csv", "high", rec.get("why", "")), row, t))
    for b in bad:
        warn(b)
    return out


async def core(mm, args, d_from, d_to, outdir, stamp):
    if args.undo:
        await undo(mm, Path(args.undo))
        return

    init = await mm.init()
    cats = Categories(init)
    rules = load_all(args, cats)
    print(f"Fetching {d_from} .. {d_to} ...")
    rows, timeouts = await mm.fetch(d_from, d_to)
    if timeouts:
        warn(f"no response for months {', '.join(timeouts)} (treated as empty; "
             f"PC Manager can hang on empty months)")
    if args.why:
        why(rows, rules, cats, args.why)
        return
    plan = plan_all(rows, rules, cats, not args.no_learn, args.auto_limit)
    if args.apply_csv:
        changes = changes_from_csv(Path(args.apply_csv), rows, cats)
        print(f"\n{len(changes)} approved rows from {args.apply_csv}")
    else:
        changes = plan.at(args.min_conf)
        if args.only_rule:
            keep = set(args.only_rule)
            changes = [c for c in changes if c[0].name in keep]
        show(plan, rows, args, outdir, stamp, args.min_conf)

    if not args.apply:
        print("\nDRY-RUN only. Review the CSVs, then re-run with --apply "
              "(or --apply-csv FILE for reviewed rows).")
        return
    if not changes:
        print("Nothing to apply.")
        return
    if len(changes) > args.max_changes and not args.yes:
        sys.exit(f"{len(changes)} changes exceeds --max-changes {args.max_changes}; "
                 f"re-run with --yes if that is intended.")

    before_assets = await mm.assets()
    before_totals = totals(rows)
    backup = outdir / f"mm_backup_{stamp}.json"
    backup.write_text(json.dumps([row for _, row, _ in changes], indent=1,
                                 ensure_ascii=False), encoding="utf-8")
    print(f"\nBackup of {len(changes)} original rows -> {backup}")

    done, errors = 0, 0
    for r, row, t in changes:
        try:
            await mm.update(row, t)
            done += 1
            if done % 25 == 0:
                print(f"   ... {done}/{len(changes)}")
        except Exception as e:
            errors += 1
            warn(f"{row['id']} {row['mbContent'][:30]!r}: {e}")
            if errors >= args.max_errors:
                print(f"Stopping after {errors} errors. Fix the cause and re-run; "
                      f"finished rows are skipped automatically.")
                break
    print(f"Applied {done}/{len(changes)}; errors {errors}")

    # ---- verify
    after_rows, _ = await mm.fetch(d_from, d_to)
    by_id = {r["id"]: r for r in after_rows}

    def at_target(r, t):
        return bool(r) and r["mcid"] == t["mcid"] and norm(r["subCategory"]) == norm(t["subCategory"])
    not_moved = [row["id"] for _, row, t in changes if not at_target(by_id.get(row["id"]), t)]
    ok_rows = len(not_moved) == len(changes) - done
    ok_totals = totals(after_rows) == before_totals
    after_assets = await mm.assets()
    ok_assets = after_assets == before_assets
    print("\nVerification")
    print(f"   rows now in target category : {len(changes) - len(not_moved)}/{len(changes)}"
          f"{'' if ok_rows else '  <-- MISMATCH'}")
    print(f"   income/expense totals/month : {'unchanged' if ok_totals else 'CHANGED!'}")
    print(f"   account balances            : {'unchanged' if ok_assets else 'CHANGED!'}")
    if not (ok_rows and ok_totals and ok_assets):
        print(f"   Something is off. Restore with:  --undo {backup}")


async def undo(mm: MM, path: Path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    print(f"Restoring {len(rows)} rows from {path}")
    for row in rows:
        t = dict(mcid=row["mcid"], mbCategory=row["mbCategory"],
                 mcscid=row["mcscid"], subCategory=row["subCategory"])
        await mm.update(row, t)
    print("Done.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server-cmd", help="command that starts your money-manager-mcp server "
                   "(or set MM_SERVER_CMD)")
    p.add_argument("--connect", metavar="HOST:PORT", help="call connection_set first if needed")
    p.add_argument("--rules", default=str(HERE / "rules.toml"), help="your rules (strict)")
    p.add_argument("--kb", default=str(HERE / "kb.toml"), help="knowledge-base rules (lenient)")
    p.add_argument("--no-kb", action="store_true", help="ignore kb.toml")
    p.add_argument("--no-learn", action="store_true", help="disable history learning + refund pairing")
    p.add_argument("--min-conf", choices=["high", "medium"], default="high",
                   help="apply only suggestions at/above this confidence (default high)")
    p.add_argument("--auto-limit", type=float, default=5000,
                   help="kb/learned picks above this amount go to review even if confident")
    p.add_argument("--mbid", default="1")
    p.add_argument("--from", dest="date_from", default="2025-01-01")
    p.add_argument("--to", dest="date_to", default=None, help="default: today")
    p.add_argument("--apply", action="store_true", help="actually write changes (default: dry-run)")
    p.add_argument("--apply-csv", metavar="REVIEW.csv", help="with --apply: apply rows you marked "
                   "approve=y in a review CSV (you may edit suggested_category/_sub)")
    p.add_argument("--only-rule", action="append", help="limit to a rule/decision name (repeatable)")
    p.add_argument("--why", metavar="TEXT", help="explain the decision for every row containing TEXT")
    p.add_argument("--unmatched", type=int, default=30, help="show N top unclassified payees")
    p.add_argument("--top-rules", type=int, default=25)
    p.add_argument("--max-errors", type=int, default=3)
    p.add_argument("--max-changes", type=int, default=800, help="safety brake; --yes to exceed")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--timeout", type=int, default=60, help="seconds per MCP call")
    p.add_argument("--outdir", default="mm_runs")
    p.add_argument("--undo", metavar="BACKUP.json", help="restore rows from a backup file")
    p.add_argument("--dump-json", help="offline test: transactions JSON dump (read-only)")
    p.add_argument("--init-json", help="offline test: init_get_data JSON dump")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
