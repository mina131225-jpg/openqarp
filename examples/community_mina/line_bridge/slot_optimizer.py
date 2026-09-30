"""条件付きシフト自動作成（古典ソルバ本体）。

日付 × 時間枠 × スタッフの割当を、貪欲法＋局所探索（classical greedy + local search）で探索し、
重みの違う3案（希望優先／人件費優先／バランス）と制約違反数を返す。
量子計算は使っていない（量子方式との比較は quantum_compare.py の小問題のみ）。
"""

from __future__ import annotations

import calendar
import random
import time
from datetime import date, timedelta
from typing import Any

import shift_rules as sr

METHOD = "classical_greedy_local_search"
METHOD_JA = "古典ソルバ（貪欲法＋局所探索）"
HONEST_COPY = "複数条件から最適化エンジン（古典ソルバ）がシフト候補を自動探索しました。"

VARIANTS = [
    ("prefer", "希望優先", {"pref": 40.0, "cost": 0.3, "fair": 4.0}),
    ("cost", "人件費優先", {"pref": 6.0, "cost": 2.5, "fair": 1.0}),
    ("balance", "バランス", {"pref": 20.0, "cost": 1.0, "fair": 12.0}),
]
VIOLATION_WEIGHT = 5000.0
LEGAL_WEEKLY_HOURS = 40.0
VIOLATION_LABELS = {
    "shortage": "人数不足",
    "unavailable": "希望外・NG日の割当",
    "ng_pair": "同じ時間NGの組み合わせ",
    "mix": "新人ベテラン不足",
    "consecutive": "連勤超過",
    "daily_limit": "1日の枠数超過",
    "weekly_hours": "週の上限時間超過",
    "cost_cap": "人件費上限超過",
}


def _wage_fields(member: dict[str, Any], store: dict[str, Any]) -> tuple[int, int]:
    wage = int(member.get("hourly_wage") or 1100)
    night = member.get("night_hourly_wage")
    if not night:
        rate = float(((store.get("wage_premiums") or {}).get("night")) or 1.25)
        night = int(round(wage * rate))
    return wage, int(night)


def build_problem(store: dict[str, Any], dates: list[str] | None = None) -> dict[str, Any]:
    rules = sr.get_rules(store)
    dates = dates or sr.planning_dates(store)
    staff = []
    for m in store.get("members") or []:
        wid = str(m.get("worker_id") or "")
        if not wid:
            continue
        wage, night = _wage_fields(m, store)
        staff.append({
            "wid": wid,
            "name": sr.label_of(store, wid),
            "wage": wage,
            "night_wage": night,
            "level": m.get("level"),
            "max_hours_week": m.get("max_hours_week"),
        })
    slots = rules["slots"]
    elig: dict[tuple[str, str, str], str] = {}
    for d in dates:
        for s in slots:
            for st in staff:
                elig[(d, s["name"], st["wid"])] = sr.eligibility(store, st["wid"], d, s, dates)
    cap = rules.get("cost_cap")
    cap_yen = None
    cap_note = None
    if cap and cap.get("yen"):
        if cap.get("period") == "week":
            cap_yen = float(cap["yen"]) * len(dates) / 7.0
            cap_note = f"週 {int(cap['yen']):,}円"
        else:
            d0 = date.fromisoformat(dates[0])
            dim = calendar.monthrange(d0.year, d0.month)[1]
            cap_yen = float(cap["yen"]) * len(dates) / dim
            cap_note = f"月 {int(cap['yen']):,}円 → この{len(dates)}日分 {int(cap_yen):,}円（日割り）"
    return {
        "dates": dates,
        "slots": slots,
        "staff": staff,
        "elig": elig,
        "any_ok": any(v == "ok" for v in elig.values()),
        "rules": rules,
        "cost_cap_yen": cap_yen,
        "cost_cap_note": cap_note,
    }


def shift_cost(st: dict[str, Any], slot: dict[str, Any]) -> float:
    h = sr.slot_hours(slot)
    n = sr.night_hours(slot["start"], slot["end"])
    return st["wage"] * (h - n) + st["night_wage"] * n


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return sr.hm_to_min(a["start"]) < sr.hm_to_min(b["end"]) and sr.hm_to_min(b["start"]) < sr.hm_to_min(a["end"])


def evaluate(problem: dict[str, Any], assign: dict[tuple[str, str], list[str]]) -> dict[str, Any]:
    """割当 → 指標（人件費・希望充足率・違反数内訳）。量子比較でも同じ評価を使う。"""
    rules = problem["rules"]
    staff = {s["wid"]: s for s in problem["staff"]}
    slots = {s["name"]: s for s in problem["slots"]}
    v = {k: 0 for k in VIOLATION_LABELS}
    cost = 0.0
    hits = total = 0
    hours: dict[str, float] = {w: 0.0 for w in staff}
    worked_days: dict[str, set[str]] = {w: set() for w in staff}
    per_day: dict[tuple[str, str], int] = {}
    max_per_day = int(rules.get("max_slots_per_day") or 1)
    ng_pairs = [tuple(p) for p in rules.get("ng_pairs") or []]
    for d in problem["dates"]:
        for sname, slot in slots.items():
            ws = assign.get((d, sname)) or []
            v["shortage"] += max(0, int(slot["required"]) - len(ws))
            for w in ws:
                if w not in staff:
                    continue
                e = problem["elig"].get((d, sname, w), "unknown")
                if e == "no":
                    v["unavailable"] += 1
                if e == "ok":
                    hits += 1
                total += 1
                cost += shift_cost(staff[w], slot)
                hours[w] += sr.slot_hours(slot)
                worked_days[w].add(d)
                per_day[(w, d)] = per_day.get((w, d), 0) + 1
            for a, b in ng_pairs:
                if a in ws and b in ws:
                    v["ng_pair"] += 1
            if rules.get("require_mix") and ws:
                lv = {staff[w].get("level") for w in ws if w in staff}
                if "新人" not in lv or "ベテラン" not in lv:
                    v["mix"] += 1
        # 同日重複（重なる枠）
        for i, (n1, s1) in enumerate(slots.items()):
            for n2, s2 in list(slots.items())[i + 1:]:
                if _overlap(s1, s2):
                    both = set(assign.get((d, n1)) or []) & set(assign.get((d, n2)) or [])
                    v["unavailable"] += len(both)
    v["daily_limit"] = sum(max(0, n - max_per_day) for n in per_day.values())
    maxc = int(rules.get("max_consecutive") or 99)
    all_dates = [date.fromisoformat(d) for d in problem["dates"]]
    for w, ds in worked_days.items():
        run = 0
        for d in all_dates:
            if d.isoformat() in ds:
                run += 1
                if run > maxc:
                    v["consecutive"] += 1
            else:
                run = 0
        cap = staff[w].get("max_hours_week") or LEGAL_WEEKLY_HOURS
        if cap:
            weeks = max(1.0, len(all_dates) / 7.0)
            if hours[w] > float(cap) * weeks + 1e-9:
                v["weekly_hours"] += 1
    if problem.get("cost_cap_yen") is not None and cost > problem["cost_cap_yen"] + 1e-6:
        v["cost_cap"] += 1
    vals = [hours[w] for w in staff] or [0.0]
    mean = sum(vals) / len(vals)
    fairness_sd = (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5
    return {
        "labor_cost": int(round(cost)),
        "pref_hits": hits,
        "assigned": total,
        "pref_rate": (hits / total) if total and problem.get("any_ok") else None,
        "violations": v,
        "violation_total": sum(v.values()),
        "hours_by_staff": {w: round(h, 2) for w, h in hours.items()},
        "fairness_sd": round(fairness_sd, 2),
    }


def objective(m: dict[str, Any], w: dict[str, float]) -> float:
    return (VIOLATION_WEIGHT * m["violation_total"] + w["cost"] * m["labor_cost"] / 100.0
            - w["pref"] * m["pref_hits"] + w["fair"] * m["fairness_sd"])


def _greedy(problem: dict[str, Any], w: dict[str, float]) -> dict[tuple[str, str], list[str]]:
    rules = problem["rules"]
    staff = problem["staff"]
    assign: dict[tuple[str, str], list[str]] = {}
    hours = {s["wid"]: 0.0 for s in staff}
    ng = {tuple(sorted(p)) for p in rules.get("ng_pairs") or []}
    for d in problem["dates"]:
        for slot in problem["slots"]:
            chosen: list[str] = []
            for _ in range(int(slot["required"])):
                best, best_score = None, None
                need_levels = set()
                if rules.get("require_mix"):
                    have = {next(s["level"] for s in staff if s["wid"] == c) for c in chosen}
                    need_levels = {"新人", "ベテラン"} - have
                for st in staff:
                    wid = st["wid"]
                    if wid in chosen:
                        continue
                    e = problem["elig"][(d, slot["name"], wid)]
                    if e == "no":
                        continue
                    if any(tuple(sorted((wid, c))) in ng for c in chosen):
                        continue
                    same_day = sum(1 for s2 in problem["slots"] if wid in (assign.get((d, s2["name"])) or []))
                    if same_day >= int(rules.get("max_slots_per_day") or 1):
                        continue
                    if any(wid in (assign.get((d, s2["name"])) or []) and _overlap(slot, s2) for s2 in problem["slots"]):
                        continue
                    score = (w["pref"] * (1 if e == "ok" else 0)
                             - w["cost"] * shift_cost(st, slot) / 100.0
                             - w["fair"] * hours[wid] / 4.0)
                    if need_levels and st.get("level") in need_levels:
                        score += 200.0
                    if best_score is None or score > best_score:
                        best, best_score = wid, score
                if best is None:
                    break
                chosen.append(best)
                hours[best] += sr.slot_hours(slot)
            assign[(d, slot["name"])] = chosen
    return assign


def _local_search(problem, assign, w, *, iters: int, seed: int):
    rng = random.Random(seed)
    cur = {k: list(v) for k, v in assign.items()}
    cur_m = evaluate(problem, cur)
    cur_obj = objective(cur_m, w)
    keys = list(cur.keys())
    wids = [s["wid"] for s in problem["staff"]]
    if not keys or not wids:
        return cur, cur_m
    for _ in range(iters):
        k = rng.choice(keys)
        cand = {kk: list(vv) for kk, vv in cur.items()}
        ws = cand[k]
        op = rng.random()
        outside = [x for x in wids if x not in ws]
        req = next(s["required"] for s in problem["slots"] if s["name"] == k[1])
        if op < 0.5 and ws and outside:
            ws[rng.randrange(len(ws))] = rng.choice(outside)
        elif op < 0.75 and outside and len(ws) < req:
            ws.append(rng.choice(outside))
        elif ws:
            ws.pop(rng.randrange(len(ws)))
        else:
            continue
        m = evaluate(problem, cand)
        o = objective(m, w)
        if o < cur_obj - 1e-9:
            cur, cur_m, cur_obj = cand, m, o
    return cur, cur_m


def generate_rule_plans(store: dict[str, Any], dates: list[str] | None = None, *, iters: int = 1500, seed: int = 7) -> dict[str, Any]:
    problem = build_problem(store, dates)
    plans = []
    for key, label, w in VARIANTS:
        t0 = time.perf_counter()
        a = _greedy(problem, w)
        a, m = _local_search(problem, a, w, iters=iters, seed=seed)
        sec = time.perf_counter() - t0
        plans.append({
            "key": key,
            "label": label,
            "assign": {f"{d}|{s}": ws for (d, s), ws in a.items()},
            "metrics": m,
            "seconds": round(sec, 4),
            "method": METHOD,
        })
    return {
        "dates": problem["dates"],
        "slots": problem["slots"],
        "rules": problem["rules"],
        "cost_cap_note": problem["cost_cap_note"],
        "plans": plans,
        "honest_note": HONEST_COPY,
        "generated_at": sr.now_jst().isoformat(timespec="seconds"),
    }


def assign_from_plan(plan: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    out = {}
    for k, ws in (plan.get("assign") or {}).items():
        d, s = k.split("|", 1)
        out[(d, s)] = list(ws)
    return out


def violations_text(m: dict[str, Any]) -> str:
    parts = [f"{VIOLATION_LABELS[k]}{n}" for k, n in m["violations"].items() if n]
    return "、".join(parts) if parts else "なし"


def pref_rate_text(m: dict[str, Any]) -> str:
    if m.get("pref_rate") is None:
        return "—（希望未提出）"
    return f"{m['pref_rate'] * 100:.0f}%（{m['pref_hits']}/{m['assigned']}）"


def schedule_lines(store: dict[str, Any], bundle: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    a = assign_from_plan(plan)
    lines = []
    for d in bundle["dates"]:
        parts = []
        for s in bundle["slots"]:
            ws = a.get((d, s["name"])) or []
            names = "・".join(sr.label_of(store, w) for w in ws) or "（なし）"
            parts.append(f"{s['name']} {names}")
        lines.append(f"{sr.date_label(d)} " + " ／ ".join(parts))
    return lines


def format_rule_plans_text(store: dict[str, Any], bundle: dict[str, Any]) -> str:
    d = bundle["dates"]
    lines = [f"【条件付き自動作成】{sr.date_label(d[0])}〜{sr.date_label(d[-1])}", bundle["honest_note"]]
    if bundle.get("cost_cap_note"):
        lines.append(f"人件費上限: {bundle['cost_cap_note']}")
    for i, p in enumerate(bundle["plans"], 1):
        m = p["metrics"]
        lines.append("")
        lines.append(f"■ 案{i} {p['label']}")
        lines.append(f"人件費 {m['labor_cost']:,}円 ／ 希望充足率 {pref_rate_text(m)} ／ 制約違反 {m['violation_total']}件（{violations_text(m)}）")
        lines.extend("  " + ln for ln in schedule_lines(store, bundle, p))
    lines.append("")
    lines.append(f"※ {METHOD_JA}で計算。量子計算は使っていません。")
    return "\n".join(lines)


def build_rule_plans_flex(bundle: dict[str, Any], *, qcompare_enabled: bool = True) -> dict[str, Any]:
    colors = {"prefer": "#166534", "cost": "#9a3412", "balance": "#1e40af"}
    bubbles = []
    for i, p in enumerate(bundle["plans"], 1):
        m = p["metrics"]
        rows = [
            ("人件費", f"{m['labor_cost']:,}円"),
            ("希望充足率", pref_rate_text(m)),
            ("制約違反", f"{m['violation_total']}件"),
            ("勤務の偏り(SD)", f"{m['fairness_sd']:g}h"),
        ]
        body = [{
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {"type": "text", "text": k, "size": "xs", "color": "#64748b", "flex": 3},
                {"type": "text", "text": v, "size": "xs", "weight": "bold", "align": "end", "flex": 4, "wrap": True},
            ],
        } for k, v in rows]
        body.append({"type": "text", "text": "内訳: " + violations_text(m), "size": "xxs", "color": "#b91c1c" if m["violation_total"] else "#166534", "wrap": True, "margin": "md"})
        footer = [
            {"type": "button", "style": "primary", "height": "sm",
             "action": {"type": "postback", "label": "この案で確定", "data": f"v=1&action=confirm_rule_plan&key={p['key']}", "displayText": f"条件案 {p['label']} で確定"}},
        ]
        if qcompare_enabled:
            footer.append({"type": "button", "style": "secondary", "height": "sm",
                           "action": {"type": "postback", "label": "⚛️ 量子で比べる", "data": f"v=1&action=qcompare&key={p['key']}", "displayText": "⚛️ 量子で比べる"}})
        footer.append({"type": "text", "text": METHOD_JA, "size": "xxs", "color": "#94a3b8", "wrap": True, "margin": "sm"})
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#f8fafc", "paddingAll": "12px",
                       "contents": [{"type": "text", "text": f"案{i} {p['label']}", "weight": "bold", "color": colors.get(p["key"], "#334155")}]},
            "body": {"type": "box", "layout": "vertical", "paddingAll": "12px", "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "10px", "contents": footer},
        })
    return {"type": "flex", "altText": "条件付きシフト3案", "contents": {"type": "carousel", "contents": bubbles}}


def dated_shifts_from_plan(bundle: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """確定保存用: {date: {"slots": [{name,start,end,workers}]}}（後から条件を変えても時刻が残る）。"""
    a = assign_from_plan(plan)
    out: dict[str, Any] = {}
    for d in bundle["dates"]:
        out[d] = {"slots": [
            {"name": s["name"], "start": s["start"], "end": s["end"], "workers": list(a.get((d, s["name"])) or [])}
            for s in bundle["slots"]
        ], "plan_key": plan["key"]}
    return out


def shifts_for_worker(store: dict[str, Any], wid: str, d: str) -> list[dict[str, Any]]:
    day = (store.get("dated_shifts") or {}).get(d) or {}
    return [s for s in day.get("slots") or [] if wid in (s.get("workers") or [])]


def worker_window(store: dict[str, Any], wid: str, d: str) -> tuple[str, str] | None:
    ss = shifts_for_worker(store, wid, d)
    if not ss:
        return None
    return (min(ss, key=lambda s: sr.hm_to_min(s["start"]))["start"], max(ss, key=lambda s: sr.hm_to_min(s["end"]))["end"])


def own_dated_shift_text(store: dict[str, Any], wid: str, *, today: date | None = None, days: int = 14) -> str | None:
    t = today or sr.now_jst().date()
    lines = []
    for i in range(days):
        d = (t + timedelta(days=i)).isoformat()
        ss = shifts_for_worker(store, wid, d)
        if ss:
            lines.append(f"{sr.date_label(d)} " + "、".join(f"{s['name']} {s['start']}〜{s['end']}" for s in ss))
    if not lines:
        return None
    return "【あなたの確定シフト】\n" + "\n".join(lines)
