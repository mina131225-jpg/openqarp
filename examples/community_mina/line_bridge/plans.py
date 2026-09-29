#!/usr/bin/env python3
"""店長向け 3 案シフト生成と予定人件費シミュレーション（PoC）。

方針（正直ラベル）:
  - 組表本体は古典ヒューリスティック（貪欲＋局所改善）。
  - 希望優先 / 人件費優先 / バランス は重み・割当順の違いで 3 案を作る。
  - 予定人件費はシフト日数 × 1シフト時間 × 時給（＋土日/祝日割増）のシミュレーション。
    給与計算・支払いは行わない。
  - QUBO/QAOA は注目日の比較フック（利用可能なときのみ）。週次全体の量子最適化ではない。
"""

from __future__ import annotations

import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_COMMUNITY = _HERE.parent
_PITCH = _COMMUNITY / "pitch"
for _p in (_PITCH, _COMMUNITY, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import shift_scheduling_demo as ssd  # noqa: E402
from stores import (  # noqa: E402
    DEFAULT_HOURLY_WAGE,
    DEFAULT_HOURS_PER_SHIFT,
    DEFAULT_WAGE_PREMIUMS,
    DEFAULT_WEEKEND_DAYS,
    hours_per_shift_for_store,
    staff_profiles_for_store,
)

PLAN_LABELS = {
    "prefer": "希望優先",
    "cost": "人件費優先",
    "balance": "バランス",
}
PLAN_ORDER = ["prefer", "cost", "balance"]

POC_FOOTER = (
    "OpenQARP（量子アプリ）で試作した店舗シフトPoC。"
    "組表本体は古典ソルバ。予定人件費はシミュレーション（給与計算ではない）。"
    "店長確認前提。"
)


def _profiles_and_meta(store: dict[str, Any] | None, scenario: dict[str, Any]) -> tuple[dict[str, dict], float, dict, list, list]:
    profiles = staff_profiles_for_store(store) if store else {}
    # シナリオ workers にプロファイルが無い枠は既定時給
    for w in scenario.get("workers") or []:
        profiles.setdefault(
            w,
            {
                "user_id": None,
                "display_name": w,
                "hourly_wage": DEFAULT_HOURLY_WAGE,
                "max_hours_week": None,
                "available_days": None,
                "role": None,
                "skills": [],
                "is_manager": False,
            },
        )
    hps = hours_per_shift_for_store(store)
    premiums = dict(DEFAULT_WAGE_PREMIUMS)
    if store and isinstance(store.get("wage_premiums"), dict):
        premiums.update({k: float(v) for k, v in store["wage_premiums"].items() if isinstance(v, (int, float))})
    weekend = list(store.get("weekend_days") or DEFAULT_WEEKEND_DAYS) if store else list(DEFAULT_WEEKEND_DAYS)
    holidays = list(store.get("holiday_days") or []) if store else []
    return profiles, hps, premiums, weekend, holidays


def _day_multiplier(day: str, premiums: dict, weekend: list[str], holidays: list[str]) -> float:
    mult = 1.0
    if day in holidays:
        mult = max(mult, float(premiums.get("holiday") or 1.0))
    if day in weekend:
        mult = max(mult, float(premiums.get("weekend") or 1.0))
    return mult


def compute_metrics(
    schedule: dict[str, list[bool]],
    scenario: dict[str, Any],
    *,
    store: dict[str, Any] | None = None,
    profiles: dict[str, dict] | None = None,
    seconds: float = 0.0,
    solver_score: float | None = None,
) -> dict[str, Any]:
    """希望充足・必要人数違反・総勤務時間・予定人件費・公平性などを算出。"""
    workers: list[str] = list(scenario["workers"])
    days: list[str] = list(scenario["days"])
    min_staff = int(scenario.get("min_staff_per_day") or 2)
    prefs = scenario.get("preferred_offs") or {}
    if profiles is None:
        profiles, hps, premiums, weekend, holidays = _profiles_and_meta(store, scenario)
    else:
        _, hps, premiums, weekend, holidays = _profiles_and_meta(store, scenario)

    # 希望休
    pref_hits = []
    for w in workers:
        for day in prefs.get(w, []):
            if day not in days:
                continue
            d_idx = days.index(day)
            granted = not schedule[w][d_idx]
            pref_hits.append({"worker": w, "day": day, "granted": granted})
    pref_ok = sum(1 for h in pref_hits if h["granted"])
    pref_all = len(pref_hits)

    # 必要人数
    daily_counts = [sum(1 for w in workers if schedule[w][d]) for d in range(len(days))]
    staffing_violations = sum(1 for c in daily_counts if c < min_staff)
    staffing_ok_days = sum(1 for c in daily_counts if c >= min_staff)

    # 勤務時間・人件費・週上限・勤務可能日
    loads = {w: sum(schedule[w]) for w in workers}
    total_shifts = sum(loads.values())
    total_hours = total_shifts * hps
    labor_cost = 0.0
    cost_breakdown: list[dict[str, Any]] = []
    hour_cap_violations = 0
    availability_violations = 0
    for w in workers:
        wage = int((profiles.get(w) or {}).get("hourly_wage") or DEFAULT_HOURLY_WAGE)
        cap = (profiles.get(w) or {}).get("max_hours_week")
        avail = (profiles.get(w) or {}).get("available_days")
        w_hours = loads[w] * hps
        if cap is not None and w_hours > float(cap) + 1e-9:
            hour_cap_violations += 1
        w_cost = 0.0
        for d_idx, day in enumerate(days):
            if not schedule[w][d_idx]:
                continue
            if isinstance(avail, list) and avail and day not in avail:
                availability_violations += 1
            mult = _day_multiplier(day, premiums, weekend, holidays)
            day_cost = wage * hps * mult
            w_cost += day_cost
        labor_cost += w_cost
        cost_breakdown.append(
            {
                "worker": w,
                "shifts": loads[w],
                "hours": w_hours,
                "hourly_wage": wage,
                "projected_cost": round(w_cost),
                "max_hours_week": cap,
            }
        )

    # 公平性: 勤務日数の分散が小さいほど高い（0〜100）
    load_vals = list(loads.values())
    if len(load_vals) >= 2:
        avg = sum(load_vals) / len(load_vals)
        var = sum((x - avg) ** 2 for x in load_vals) / len(load_vals)
        # 最大想定分散 ≈ (n/2 差)^2 程度で正規化
        fairness = max(0.0, min(100.0, 100.0 - var * 12.0))
    else:
        fairness = 100.0

    if solver_score is None:
        try:
            solver_score = float(ssd.score_schedule(schedule, scenario))
        except Exception:  # noqa: BLE001
            solver_score = 0.0

    return {
        "pref_ok": pref_ok,
        "pref_all": pref_all,
        "pref_rate": (pref_ok / pref_all) if pref_all else 1.0,
        "pref_hits": pref_hits,
        "staffing_violations": staffing_violations,
        "staffing_ok_days": staffing_ok_days,
        "staffing_days": len(days),
        "daily_counts": daily_counts,
        "total_shifts": total_shifts,
        "total_hours": round(total_hours, 1),
        "hours_per_shift": hps,
        "projected_labor_cost": int(round(labor_cost)),
        "cost_breakdown": cost_breakdown,
        "fairness": round(fairness, 1),
        "loads": loads,
        "hour_cap_violations": hour_cap_violations,
        "availability_violations": availability_violations,
        "solver_score": round(float(solver_score), 2),
        "seconds": round(float(seconds), 6),
        "method": "classical_greedy_heuristic",
    }


def _greedy_variant(
    scenario: dict[str, Any],
    *,
    mode: str,
    profiles: dict[str, dict],
    hours_per_shift: float,
) -> tuple[dict[str, list[bool]], float, float]:
    """mode=prefer|cost|balance の貪欲＋局所改善。"""
    started = time.perf_counter()
    workers = list(scenario["workers"])
    days = list(scenario["days"])
    min_staff = int(scenario["min_staff_per_day"])
    max_consec = int(scenario["max_consecutive_days"])
    prefs = scenario.get("preferred_offs") or {}

    schedule = {w: [False] * len(days) for w in workers}

    def wage_of(w: str) -> int:
        return int((profiles.get(w) or {}).get("hourly_wage") or DEFAULT_HOURLY_WAGE)

    def cap_remaining(w: str) -> float:
        cap = (profiles.get(w) or {}).get("max_hours_week")
        if cap is None:
            return 1e9
        used = sum(schedule[w]) * hours_per_shift
        return float(cap) - used

    def can_work(w: str, day: str) -> bool:
        avail = (profiles.get(w) or {}).get("available_days")
        if isinstance(avail, list) and avail and day not in avail:
            return False
        if cap_remaining(w) < hours_per_shift - 1e-9:
            return False
        return True

    for d_idx, day in enumerate(days):
        def rank_key(w: str):
            pref_pen = 1 if day in prefs.get(w, []) else 0
            load = sum(schedule[w][:d_idx])
            wage = wage_of(w)
            avail_pen = 0 if can_work(w, day) else 1
            if mode == "prefer":
                # 希望休を最優先で避ける → 希望者は後回し
                return (avail_pen, pref_pen, load, wage, w)
            if mode == "cost":
                # 安い人を先に、希望は弱めに考慮
                return (avail_pen, wage, pref_pen, load, w)
            # balance: 負荷均等を優先しつつ希望も見る
            return (avail_pen, load, pref_pen, wage, w)

        ranked = sorted(workers, key=rank_key)
        for w in ranked:
            if sum(1 for x in workers if schedule[x][d_idx]) >= min_staff:
                break
            if not can_work(w, day):
                continue
            run = 0
            for on in reversed(schedule[w][:d_idx]):
                if on:
                    run += 1
                else:
                    break
            if run >= max_consec:
                continue
            # prefer: 希望休の日は可能ならスキップ（人数足りる見込みがあるとき）
            if mode == "prefer" and day in prefs.get(w, []):
                others = [
                    x for x in ranked
                    if x != w and can_work(x, day) and not schedule[x][d_idx]
                ]
                if len(others) + sum(1 for x in workers if schedule[x][d_idx]) >= min_staff:
                    continue
            schedule[w][d_idx] = True

        # まだ足りなければ制約を緩めて埋める（可用・希望を多少無視）
        for w in ranked:
            if sum(1 for x in workers if schedule[x][d_idx]) >= min_staff:
                break
            if cap_remaining(w) < hours_per_shift - 1e-9:
                continue
            schedule[w][d_idx] = True

    # 局所改善: 目的に応じたスコア
    def local_score(sch: dict[str, list[bool]]) -> float:
        base = ssd.score_schedule(sch, scenario)
        # 人件費項（安いほど加点）— 正規化のため万円単位で減点
        cost = 0.0
        for w in workers:
            wage = wage_of(w)
            for d_idx, day in enumerate(days):
                if sch[w][d_idx]:
                    # 簡易: 割増なしで局所改善（最終 metrics で割増込み）
                    cost += wage * hours_per_shift
        cost_term = cost / 10000.0
        # 公平性
        loads = [sum(sch[w]) for w in workers]
        avg = sum(loads) / len(loads)
        fair_pen = sum((x - avg) ** 2 for x in loads)
        # 週上限・可用違反
        cap_pen = 0.0
        for w in workers:
            cap = (profiles.get(w) or {}).get("max_hours_week")
            if cap is not None and sum(sch[w]) * hours_per_shift > float(cap) + 1e-9:
                cap_pen += 80.0
            avail = (profiles.get(w) or {}).get("available_days")
            if isinstance(avail, list) and avail:
                for d_idx, day in enumerate(days):
                    if sch[w][d_idx] and day not in avail:
                        cap_pen += 40.0
        if mode == "prefer":
            return base - 0.15 * cost_term - fair_pen - cap_pen
        if mode == "cost":
            return base - 1.8 * cost_term - 0.5 * fair_pen - cap_pen
        return base - 0.6 * cost_term - 1.2 * fair_pen - cap_pen

    best = local_score(schedule)
    improved = True
    passes = 0
    while improved and passes < 4:
        improved = False
        passes += 1
        for w in workers:
            for d_idx in range(len(days)):
                schedule[w][d_idx] = not schedule[w][d_idx]
                new_score = local_score(schedule)
                if new_score > best:
                    best = new_score
                    improved = True
                else:
                    schedule[w][d_idx] = not schedule[w][d_idx]

    elapsed = time.perf_counter() - started
    return schedule, best, elapsed


def _serialize_schedule(schedule: dict[str, list[bool]]) -> dict[str, list[bool]]:
    return {w: [bool(x) for x in bits] for w, bits in schedule.items()}


def _plan_dict(
    key: str,
    schedule: dict[str, list[bool]],
    scenario: dict[str, Any],
    *,
    store: dict[str, Any] | None,
    profiles: dict[str, dict],
    seconds: float,
    solver_score: float,
) -> dict[str, Any]:
    metrics = compute_metrics(
        schedule,
        scenario,
        store=store,
        profiles=profiles,
        seconds=seconds,
        solver_score=solver_score,
    )
    return {
        "key": key,
        "label": PLAN_LABELS.get(key, key),
        "schedule": _serialize_schedule(schedule),
        "metrics": metrics,
        "method": "classical_greedy_heuristic",
        "method_note": "古典貪欲＋局所改善（重み違い）。量子計算ではない。",
    }


def generate_three_plans(
    scenario: dict[str, Any],
    store: dict[str, Any] | None = None,
    *,
    budget_yen: int | None = None,
) -> dict[str, Any]:
    """希望優先 / 人件費優先 / バランス の 3 案と before/after 比較用ベースを返す。"""
    sc = deepcopy(scenario)
    # ソルバ用メタを除去しても良いが、validate は _display_labels を無視するのでそのままでも可
    ssd.validate_scenario({k: v for k, v in sc.items() if not str(k).startswith("_")})
    profiles, hps, _, _, _ = _profiles_and_meta(store, sc)

    # ベースライン: デモ既存の classical_greedy（最適化「前」相当）
    t0 = time.perf_counter()
    base_sched, base_score, base_sec = ssd.classical_greedy(sc)
    base_sec = base_sec or (time.perf_counter() - t0)
    baseline = _plan_dict(
        "baseline",
        base_sched,
        sc,
        store=store,
        profiles=profiles,
        seconds=base_sec,
        solver_score=base_score,
    )
    baseline["label"] = "最適化前（既定古典）"
    baseline["key"] = "baseline"

    plans: list[dict[str, Any]] = []
    for key in PLAN_ORDER:
        sched, score, sec = _greedy_variant(sc, mode=key, profiles=profiles, hours_per_shift=hps)
        plans.append(
            _plan_dict(key, sched, sc, store=store, profiles=profiles, seconds=sec, solver_score=score)
        )

    # 予算指定時は人件費優先をさらに寄せ、超過案にフラグ
    if budget_yen is not None and budget_yen > 0:
        for p in plans:
            p["within_budget"] = p["metrics"]["projected_labor_cost"] <= budget_yen
            p["budget_yen"] = budget_yen
        # 予算内が無ければ cost 案を再生成（よりコスト重視 — 既に cost があるので注記のみ）
        if not any(p.get("within_budget") for p in plans):
            for p in plans:
                p["budget_note"] = f"予算 {budget_yen:,}円 以内の案はありません（最安は人件費優先）。"

    comparisons = []
    for p in plans:
        comparisons.append(compare_metrics(baseline["metrics"], p["metrics"], label=p["label"]))

    # 任意: 注目日 QAOA 比較フック（週次量子最適化ではない）
    quantum_compare = try_qaoa_focus_compare(sc)

    return {
        "scenario": sc,
        "baseline": baseline,
        "plans": plans,
        "comparisons": comparisons,
        "budget_yen": budget_yen,
        "quantum_compare": quantum_compare,
        "honest_note": POC_FOOTER,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def compare_metrics(before: dict[str, Any], after: dict[str, Any], *, label: str = "") -> dict[str, Any]:
    """最適化前後の指標差分。"""
    def delta(key: str) -> dict[str, Any]:
        b, a = before.get(key), after.get(key)
        if isinstance(b, (int, float)) and isinstance(a, (int, float)):
            return {"before": b, "after": a, "delta": a - b}
        return {"before": b, "after": a, "delta": None}

    cost_d = delta("projected_labor_cost")
    pref_d = delta("pref_ok")
    viol_d = delta("staffing_violations")
    fair_d = delta("fairness")
    hours_d = delta("total_hours")
    improved = []
    if (cost_d["delta"] or 0) < 0:
        improved.append(f"予定人件費 {int(cost_d['before']):,}→{int(cost_d['after']):,}円")
    if (pref_d["delta"] or 0) > 0:
        improved.append(f"希望休 {pref_d['before']}→{pref_d['after']}")
    if (viol_d["delta"] or 0) < 0:
        improved.append(f"人数不足日 {viol_d['before']}→{viol_d['after']}")
    if (fair_d["delta"] or 0) > 0:
        improved.append(f"公平性 {fair_d['before']}→{fair_d['after']}")
    return {
        "label": label,
        "labor_cost": cost_d,
        "pref_ok": pref_d,
        "staffing_violations": viol_d,
        "fairness": fair_d,
        "total_hours": hours_d,
        "improved_summary": improved,
    }


def try_qaoa_focus_compare(scenario: dict[str, Any]) -> dict[str, Any]:
    """注目日のみ QAOA Max-Cut 比較。失敗時はスタブ。週次量子最適化ではない。"""
    out: dict[str, Any] = {
        "attempted": True,
        "scope": "focus_day_only",
        "scope_note": (
            "QUBO/QAOA は注目日の出勤/休み分割の比較用。"
            "週次シフト全体の量子最適化ではない。"
        ),
        "available": False,
    }
    try:
        from solver_bridge import run_focus_exact, run_qaoa_focus, qarp_available
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"solver_bridge import failed: {exc}"
        return out

    if not qarp_available():
        out["error"] = "OpenQARP (qarp) を import できないため古典比較のみ。"
        return out

    try:
        exact = run_focus_exact(scenario)
        quantum = run_qaoa_focus(scenario, shots=500, seed=1234)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"QAOA 実行失敗: {exc}"
        return out

    if not quantum.get("available"):
        out["error"] = quantum.get("error") or "QAOA unavailable"
        return out

    out.update(
        {
            "available": True,
            "method": "QAOA Max-Cut (OpenQARP qarp)",
            "focus_day": scenario.get("qaoa_focus_day"),
            "classical_exact": {
                "method": "classical_enumeration_focus",
                "on": exact.get("on"),
                "off": exact.get("off"),
                "seconds": exact.get("seconds"),
                "pref_score": exact.get("pref_score"),
            },
            "qaoa": {
                "method": "QAOA Max-Cut",
                "on": quantum.get("qaoa_on"),
                "off": quantum.get("qaoa_off"),
                "seconds": quantum.get("qaoa_seconds"),
                "energy": quantum.get("qaoa_energy"),
                "best_cut": quantum.get("qaoa_best_cut"),
                "pref_score": quantum.get("pref_score"),
                "agree_with_exact": quantum.get("agree_exact"),
            },
            "cut_exact": {
                "method": "classical_maxcut_exact",
                "cut": quantum.get("cut_exact"),
                "seconds": quantum.get("cut_seconds"),
            },
        }
    )
    return out


def pick_plan(bundle: dict[str, Any], selector: str | None = None) -> dict[str, Any] | None:
    """確定用: '1'|'希望'|'cost'|'バランス' などから案を選ぶ。既定はバランス。"""
    plans = bundle.get("plans") or []
    if not plans:
        return None
    raw = (selector or "").strip()
    if not raw:
        return next((p for p in plans if p["key"] == "balance"), plans[0])
    # 番号
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(plans):
            return plans[idx]
    # キー・ラベル
    aliases = {
        "希望": "prefer",
        "希望優先": "prefer",
        "prefer": "prefer",
        "人件費": "cost",
        "人件費優先": "cost",
        "コスト": "cost",
        "cost": "cost",
        "バランス": "balance",
        "balance": "balance",
        "普通": "balance",
    }
    key = aliases.get(raw.lower() if raw.isascii() else raw, aliases.get(raw))
    if key:
        return next((p for p in plans if p["key"] == key), None)
    return next((p for p in plans if p["key"] == "balance"), plans[0])


def replan_lower_cost(
    scenario: dict[str, Any],
    store: dict[str, Any] | None = None,
    *,
    budget_yen: int | None = None,
) -> dict[str, Any]:
    """人件費を下げて再計算 — cost 寄せの 3 案（予算あればフラグ）。"""
    bundle = generate_three_plans(scenario, store, budget_yen=budget_yen)
    # 並びを人件費順でも付与
    ordered = sorted(bundle["plans"], key=lambda p: p["metrics"]["projected_labor_cost"])
    bundle["plans_by_cost"] = [p["key"] for p in ordered]
    bundle["cheapest_key"] = ordered[0]["key"] if ordered else None
    return bundle


def labor_cost_summary(
    scenario: dict[str, Any],
    store: dict[str, Any] | None = None,
    *,
    schedule: dict[str, list[bool]] | None = None,
) -> dict[str, Any]:
    """今週の予定人件費サマリ（確定案があればそれ、なければ古典1本）。"""
    sc = deepcopy(scenario)
    profiles, hps, _, _, _ = _profiles_and_meta(store, sc)
    if schedule is None:
        if store and isinstance(store.get("confirmed_plan"), dict):
            schedule = store["confirmed_plan"].get("schedule")
        if schedule is None:
            schedule, score, sec = ssd.classical_greedy(sc)
            metrics = compute_metrics(schedule, sc, store=store, profiles=profiles, seconds=sec, solver_score=score)
            return {
                "source": "classical_live",
                "schedule": _serialize_schedule(schedule),
                "metrics": metrics,
                "note": "未確定のため既定古典ソルバの試算です。",
            }
    metrics = compute_metrics(schedule, sc, store=store, profiles=profiles)
    source = "confirmed" if store and store.get("confirmed_plan") else "provided"
    return {
        "source": source,
        "schedule": _serialize_schedule(schedule),
        "metrics": metrics,
        "note": "確定シフトに基づく予定人件費シミュレーションです。" if source == "confirmed" else "試算です。",
    }


def metrics_one_liner(m: dict[str, Any]) -> str:
    return (
        f"希望休 {m['pref_ok']}/{m['pref_all']} ／ "
        f"人数不足 {m['staffing_violations']}日 ／ "
        f"総時間 {m['total_hours']:g}h ／ "
        f"予定人件費 {m['projected_labor_cost']:,}円 ／ "
        f"公平性 {m['fairness']:g}"
    )


def format_plans_text(bundle: dict[str, Any], *, store_name: str | None = None) -> str:
    lines: list[str] = []
    title = "シフト3案"
    if store_name:
        title = f"「{store_name}」の{title}"
    lines.append(f"【{title}】")
    lines.append("※ 古典ソルバの重み違いです（量子計算ではありません）")
    base = bundle.get("baseline") or {}
    bm = base.get("metrics") or {}
    if bm:
        lines.append("")
        lines.append("■ 最適化前（既定古典）")
        lines.append(metrics_one_liner(bm))
    for i, p in enumerate(bundle.get("plans") or [], 1):
        m = p["metrics"]
        lines.append("")
        lines.append(f"■ 案{i} {p['label']}（{p['method']}）")
        lines.append(metrics_one_liner(m))
        flag = ""
        if "within_budget" in p:
            flag = " ✅予算内" if p["within_budget"] else " ⚠予算超"
        if flag:
            lines.append(f"  {flag}")
        # before/after
        comps = bundle.get("comparisons") or []
        if i - 1 < len(comps):
            imp = comps[i - 1].get("improved_summary") or []
            cost_d = comps[i - 1].get("labor_cost") or {}
            d = cost_d.get("delta")
            if d is not None:
                sign = "↓" if d < 0 else ("↑" if d > 0 else "→")
                lines.append(f"  最適化前比 人件費 {sign} {abs(int(d)):,}円")
            if imp:
                lines.append("  改善: " + "、".join(imp))
    qc = bundle.get("quantum_compare") or {}
    lines.append("")
    if qc.get("available"):
        lines.append(
            f"■ 参考: 注目日({qc.get('focus_day')}) QAOA Max-Cut 比較 "
            f"（週次量子最適化ではない）"
        )
        ce = qc.get("classical_exact") or {}
        q = qc.get("qaoa") or {}
        lines.append(
            f"  古典列挙 pref={ce.get('pref_score')} {ce.get('seconds', 0)*1000:.1f}ms ／ "
            f"QAOA pref={q.get('pref_score')} {float(q.get('seconds') or 0)*1000:.1f}ms "
            f"一致={q.get('agree_with_exact')}"
        )
    else:
        lines.append("■ 参考: 注目日 QAOA 比較は未実行またはスキップ")
        if qc.get("error"):
            lines.append(f"  ({qc.get('error')})")
    lines.append("")
    lines.append("確定する案を選んで「確定」「確定 希望」「確定 2」などと送ってください。")
    lines.append(f"※ {POC_FOOTER}")
    return "\n".join(lines)


def build_plans_flex(bundle: dict[str, Any], *, alt_text: str | None = None) -> dict[str, Any]:
    """3 案の Flex カルーセル。"""
    bubbles = []
    colors = {"prefer": "#166534", "cost": "#9a3412", "balance": "#1e40af"}
    for i, p in enumerate(bundle.get("plans") or [], 1):
        m = p["metrics"]
        color = colors.get(p["key"], "#334155")
        rows = [
            ("希望休達成", f"{m['pref_ok']}/{m['pref_all']}"),
            ("必要人数不足", f"{m['staffing_violations']}日"),
            ("総勤務時間", f"{m['total_hours']:g}h"),
            ("予定人件費", f"{m['projected_labor_cost']:,}円"),
            ("公平性", f"{m['fairness']:g}"),
        ]
        body_contents = []
        for label, val in rows:
            body_contents.append(
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": label, "size": "xs", "color": "#64748b", "flex": 3},
                        {"type": "text", "text": val, "size": "xs", "weight": "bold", "align": "end", "flex": 3},
                    ],
                    "margin": "sm",
                }
            )
        # before/after cost
        comps = bundle.get("comparisons") or []
        if i - 1 < len(comps):
            d = (comps[i - 1].get("labor_cost") or {}).get("delta")
            if d is not None:
                sign = "↓" if d < 0 else ("↑" if d > 0 else "→")
                body_contents.append(
                    {
                        "type": "text",
                        "text": f"最適化前比 人件費 {sign}{abs(int(d)):,}円",
                        "size": "xxs",
                        "color": "#166534" if d < 0 else "#64748b",
                        "margin": "md",
                        "wrap": True,
                    }
                )
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"案{i} {p['label']}",
                            "weight": "bold",
                            "size": "md",
                            "color": color,
                        },
                        {
                            "type": "text",
                            "text": "classical_greedy_heuristic",
                            "size": "xxs",
                            "color": "#94a3b8",
                            "margin": "sm",
                        },
                    ],
                    "paddingAll": "12px",
                    "backgroundColor": "#f8fafc",
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": body_contents,
                    "paddingAll": "12px",
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"「確定 {i}」または「確定 {p['label']}」",
                            "size": "xxs",
                            "color": "#64748b",
                            "wrap": True,
                        },
                        {
                            "type": "text",
                            "text": POC_FOOTER,
                            "size": "xxs",
                            "color": "#94a3b8",
                            "wrap": True,
                            "margin": "sm",
                        },
                    ],
                    "paddingAll": "10px",
                },
            }
        )

    return {
        "type": "flex",
        "altText": alt_text or f"シフト3案｜{POC_FOOTER}",
        "contents": {"type": "carousel", "contents": bubbles},
    }


def format_labor_cost_text(summary: dict[str, Any], *, store_name: str | None = None) -> str:
    m = summary["metrics"]
    title = "今週の予定人件費"
    if store_name:
        title = f"「{store_name}」の{title}"
    lines = [
        f"【{title}】",
        f"出典: {summary.get('note') or summary.get('source')}",
        metrics_one_liner(m),
        "",
        "内訳（予定）:",
    ]
    labels = {}
    for row in m.get("cost_breakdown") or []:
        w = row["worker"]
        lines.append(
            f"  {w}: {row['shifts']}日出勤 / {row['hours']:g}h / "
            f"時給{row['hourly_wage']}円 → {row['projected_cost']:,}円"
        )
    lines.append("")
    lines.append(f"※ {POC_FOOTER}")
    return "\n".join(lines)


def format_own_shift_text(
    schedule: dict[str, list[bool]],
    scenario: dict[str, Any],
    worker_id: str,
    *,
    display_name: str | None = None,
) -> str:
    days = scenario["days"]
    prefs = scenario.get("preferred_offs") or {}
    label = display_name or worker_id
    if worker_id not in schedule:
        return f"{label} の枠がスケジュールにありません。"
    bits = schedule[worker_id]
    lines = [f"【{label} のシフト】"]
    for d_idx, day in enumerate(days):
        on = bits[d_idx]
        mark = "出勤" if on else "休み"
        if day in prefs.get(worker_id, []):
            mark = "休み✓（希望）" if not on else "出勤⚠（希望休）"
        lines.append(f"  {day}: {mark}")
    lines.append("")
    lines.append(f"※ {POC_FOOTER}")
    return "\n".join(lines)


def bundle_for_storage(bundle: dict[str, Any]) -> dict[str, Any]:
    """stores.json に載せる軽量版（巨大化防止）。"""
    def thin(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": p["key"],
            "label": p["label"],
            "schedule": p["schedule"],
            "metrics": {
                k: p["metrics"][k]
                for k in (
                    "pref_ok", "pref_all", "staffing_violations", "total_hours",
                    "projected_labor_cost", "fairness", "loads", "solver_score",
                    "seconds", "method", "hour_cap_violations", "availability_violations",
                )
                if k in p["metrics"]
            },
            "method": p.get("method"),
            "method_note": p.get("method_note"),
        }
    return {
        "generated_at": bundle.get("generated_at"),
        "baseline": thin(bundle["baseline"]) if bundle.get("baseline") else None,
        "plans": [thin(p) for p in bundle.get("plans") or []],
        "comparisons": bundle.get("comparisons") or [],
        "budget_yen": bundle.get("budget_yen"),
        "quantum_compare": {
            "available": (bundle.get("quantum_compare") or {}).get("available"),
            "method": (bundle.get("quantum_compare") or {}).get("method"),
            "scope": (bundle.get("quantum_compare") or {}).get("scope"),
            "error": (bundle.get("quantum_compare") or {}).get("error"),
        },
        "honest_note": bundle.get("honest_note"),
    }
