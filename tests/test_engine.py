"""Synthetic-data tests. No real transactions are used anywhere in this repo."""
import asyncio
import argparse
import copy
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mm_recategorize as m  # noqa: E402


def tree():
    def cat(i, name, subs=()):
        return {"mcid": f"C{i}", "mcname": name,
                "mcsc": [{"mcscid": f"C{i}S{j}", "mcscname": s} for j, s in enumerate(subs)]}
    return {"categories": {
        "expense": [cat(1, "🍜 Food", ["Eating out"]), cat(2, "Groceries", ["Quick Commerce", "Milk & Dairy"]),
                    cat(3, "Other"), cat(4, "Loans", ["Interest"]), cat(5, "Bank Charges"),
                    cat(6, "🚖 Transport", ["Taxi", "Bus"]), cat(11, "Utilities", ["Mobile"]),
                    cat(12, "Household", ["Electricity"])],
        "income": [cat(7, "Other"), cat(8, "Refunds", ["Shopping"]), cat(9, "📈 Trading"),
                   cat(10, "Trading", ["Profit"])]}}


_n = 0


def row(title, amt=100.0, code="1", cat="Other", sub="", date="2026-01-10", pay="ICICI Bank", note=""):
    global _n
    _n += 1
    return {"id": f"{_n:08X}-0000-4000-8000-{_n:012X}", "mbDate": date, "assetId": "A1", "payType": pay,
            "mcid": "x", "mbCategory": cat, "mcscid": "x", "subCategory": sub, "mbContent": title,
            "mbCash": amt, "inOutCode": code, "inOutType": "Exp.", "mbDetailContent": note}


def load(rules_toml: str, tmp_path):
    cats = m.Categories(tree())
    p = tmp_path / "r.toml"
    p.write_text(rules_toml)
    return cats, m.load_rules(p, cats, "user", strict=True)


def test_norm_and_duplicate_categories():
    assert m.norm("🍜 Food ") == "food"
    cats = m.Categories(tree())
    assert cats.resolve("income", "Trading", "Profit")["mcid"] == "C10"   # picks the one with the sub
    assert cats.resolve("income", "trading")["mcid"] == "C9"
    assert not cats.exists("expense", "Groceries", "Nope")


def test_default_only_moves_uncategorised(tmp_path):
    cats, rules = load('[[rule]]\nname="z"\nmatch="ZEPTO"\ncategory="Groceries"\nsubcategory="Quick Commerce"\n', tmp_path)
    rows = [row("ZEPTO 1"), row("ZEPTO 2", cat="Food"), row("ZEPTO 3", cat="Groceries")]
    plan = m.plan_all(rows, rules, cats, learn=False)
    moved = {r["mbContent"] for _, r, _ in plan.changes}
    assert moved == {"ZEPTO 1", "ZEPTO 3"}          # 3 = right category, just missing the sub
    assert plan.protected["z"] == 1


def test_override_and_also_from(tmp_path):
    cats, rules = load('[[rule]]\nname="o"\nmatch="X"\ncategory="Groceries"\noverride=true\n', tmp_path)
    plan = m.plan_all([row("X", cat="Food")], rules, cats, learn=False)
    assert len(plan.changes) == 1


def test_transfers_never_touched(tmp_path):
    cats, rules = load('[[rule]]\nname="a"\nmatch=".*"\ncategory="Groceries"\noverride=true\n', tmp_path)
    plan = m.plan_all([row("anything", code="3"), row("anything", code="4"), row("x", code="7")],
                      rules, cats, learn=False)
    assert plan.changes == []


def test_flag_rule_never_changes(tmp_path):
    cats, rules = load('[[rule]]\nname="f"\nmatch="CARD BILL"\nflag="transfer"\n', tmp_path)
    plan = m.plan_all([row("CARD BILL 123")], rules, cats, learn=False)
    assert plan.changes == [] and len(plan.flagged) == 1


def test_user_rule_beats_kb(tmp_path):
    cats = m.Categories(tree())
    user = tmp_path / "u.toml"
    user.write_text('[[rule]]\nname="u"\nmatch="SWIGGY"\ncategory="Groceries"\n')
    kb = tmp_path / "k.toml"
    kb.write_text('[[rule]]\nname="k"\nmatch="SWIGGY"\ncategory="Food"\nsubcategory="Eating out"\n'
                  '[[rule]]\nname="missing cat"\nmatch="Q"\ncategory="Does Not Exist"\n')
    rules = m.load_rules(user, cats, "user", True) + m.load_rules(kb, cats, "kb", False)
    assert len(rules) == 2                                    # kb rule with unknown category skipped
    plan = m.plan_all([row("SWIGGY ORDER")], rules, cats, learn=False)
    assert plan.changes[0][2]["mbCategory"] == "Groceries"


def test_kb_has_no_known_false_positives():
    cats = m.Categories(tree())
    kb = m.load_rules(ROOT / "kb.toml", cats, "kb", False)
    for title in ("UPI: SRIRAMULA Happy Birt", "UPI: Killada Vi", "UPI: CHAITANYA K", "UPI: YOGANANDA"):
        plan = m.plan_all([row(title)], kb, cats, learn=False)
        assert plan.changes == [], title


def test_learner_confidence_and_brake():
    hist = [row("SHOP ONE", cat="Groceries", sub="Milk & Dairy") for _ in range(4)]
    cats = m.Categories(tree())
    plan = m.plan_all(hist + [row("SHOP ONE", 50), row("SHOP ONE", 9000)], [], cats)
    by = {r["mbCash"]: d for d, r, _ in plan.changes if r["mbContent"] == "SHOP ONE" and m.is_uncat(r)}
    assert by[50].conf == "high"
    assert by[9000].conf == "medium"                          # > auto-limit goes to review


def test_learner_ignores_split_history():
    hist = [row("MIXED", cat="Groceries") for _ in range(2)] + [row("MIXED", cat="Food") for _ in range(2)]
    plan = m.plan_all(hist + [row("MIXED")], [], m.Categories(tree()))
    assert plan.changes == []


def test_refund_pairing():
    exp = row("FLIPKART PVT LTD", 900, cat="Groceries", date="2026-01-01")
    cr = row("Credit: FLIPKART PVT LTD", 900, code="0", date="2026-01-20")
    plan = m.plan_all([exp, cr], [], m.Categories(tree()))
    assert [(d.source, t["subCategory"]) for d, _, t in plan.changes] == [("pair", "Shopping")]


def test_audit_finds_minority():
    rows = [row("CARD INTEREST", cat="Loans", sub="Interest") for _ in range(9)] + \
           [row("CARD INTEREST", cat="Bank Charges")]
    plan = m.plan_all(rows, [], m.Categories(tree()))
    assert len(plan.inconsistent) == 1 and plan.inconsistent[0][2]["mbCategory"] == "Loans"


# ----------------------------------------------------------------- end-to-end with a fake server
class Fake:
    def __init__(self, rows):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.writes = 0

    async def init(self): return tree()

    async def assets(self):
        t = {}
        for r in self.rows.values():
            s = 1 if r["inOutCode"] in "04" else -1
            t[r["assetId"]] = round(t.get(r["assetId"], 0) + s * float(r["mbCash"]), 2)
        return t

    async def fetch(self, a, b): return [copy.deepcopy(r) for r in self.rows.values()], []

    async def update(self, row, t):
        self.rows[row["id"]].update(mcid=t["mcid"], mbCategory=t["mbCategory"],
                                    mcscid=t["mcscid"], subCategory=t["subCategory"])
        self.writes += 1


def args(tmp_path, **k):
    d = dict(undo=None, rules=str(tmp_path / "rules.toml"), kb=str(ROOT / "kb.toml"), no_kb=False,
             no_learn=False, min_conf="high", auto_limit=5000, only_rule=None, unmatched=0, top_rules=5,
             apply=True, apply_csv=None, why=None, max_errors=3, max_changes=800, yes=False)
    d.update(k)
    return argparse.Namespace(**d)


def run(fake, tmp_path, **k):
    D = (dt.date(2025, 1, 1), dt.date(2026, 12, 31))
    asyncio.run(m.core(fake, args(tmp_path, **k), *D, tmp_path, "t"))


def test_apply_verify_idempotent_undo(tmp_path, capsys):
    rows = [row("SWIGGY ORDER", 250), row("UBER TRIP", 120), row("salary", code="0"), row("x", code="3")]
    f = Fake(rows)
    run(f, tmp_path, apply=False)
    assert f.writes == 0                                      # dry-run writes nothing
    run(f, tmp_path)
    out = capsys.readouterr().out
    assert f.writes == 2 and "unchanged" in out and "MISMATCH" not in out
    run(f, tmp_path)
    assert f.writes == 2                                      # idempotent
    asyncio.run(m.undo(f, tmp_path / "mm_backup_t.json"))
    assert all(m.is_uncat(r) for r in f.rows.values() if r["inOutCode"] in "01" and r["mbContent"] != "salary")


def test_server_that_lies_is_caught(tmp_path, capsys):
    class Liar(Fake):
        async def update(self, row, t): pass
    run(Liar([row("SWIGGY ORDER", 250)]), tmp_path)
    assert "MISMATCH" in capsys.readouterr().out


# ----------------------------------------------------------------- regional pack (Hyderabad / Telangana)
def regional(tmp_path=None):
    cats = m.Categories(tree())
    return cats, m.load_rules(ROOT / "kb_hyderabad.toml", cats, "kb", False) + \
        m.load_rules(ROOT / "kb.toml", cats, "kb", False)


def test_regional_no_false_positives():
    cats, rules = regional()
    for title in ("NETFLIX MUMBAI IN [9003]", "BASHA BABAI MUNTHA MASALA", "GMR HOSPITALITY LIMITED",
                  "UPI: Killada Vi", "UPI: CHAITANYA K", "FRESH FRUIT JUICE POINT x",
                  "CRED marriage loan interest 4/9", "UPI: SRIRAMULA Happy Birt"):
        plan = m.plan_all([row(title)], [r for r in rules if r.conf == "high"], cats, learn=False)
        assert all(r["mbContent"] != title or d.source == "kb" and d.conf != "high"
                   for d, r, _ in plan.changes), title
    cats2, rules2 = regional()
    for title in ("BASHA BABAI MUNTHA MASALA", "NETFLIX MUMBAI IN [9003]"):
        got = [(r.name, t["mbCategory"]) for r in rules2 if r.matches(row(title))
               for t in [r.target] if t]
        assert not any(n.startswith("reg Household: maid") for n, _ in got), got


def test_regional_positives():
    cats, rules = regional()
    exp = {"HMWSSB WATER BILL": "Utilities", "TGSPDCL BILL PAY": "Household", "PARADISE FOOD COURT": "Food",
           "UPI: Rapido": "🚖 Transport", "Metro L&T Metro Rail Hyderabad": "🚖 Transport"}
    for title, want in exp.items():
        plan = m.plan_all([row(title)], rules, cats, learn=False)
        assert plan.changes and m.norm(plan.changes[0][2]["mbCategory"]) == m.norm(want), title


def test_history_beats_generic_kb():
    hist = [row("GITHUB INC", cat="Groceries") for _ in range(4)]       # your own convention
    cats, rules = regional()
    plan = m.plan_all(hist + [row("GITHUB INC", 5)], rules, cats)
    new = [(d, t) for d, r, t in plan.changes if r["mbContent"] == "GITHUB INC" and m.is_uncat(r)]
    assert new and new[0][0].source == "learned"
