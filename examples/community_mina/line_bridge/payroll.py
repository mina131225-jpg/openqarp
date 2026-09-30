#!/usr/bin/env python3
"""給与計算・見込み管理（PoC）。

銀行振込・支払いは行わない。古典計算（classical_payroll_poc）。
計算根拠（formula / hours / premiums / allowances）を永続化し、実績編集後に再計算可能。
"""

from __future__ import annotations

import csv
import json
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stores import (
    DEFAULT_HOURLY_WAGE,
    DEFAULT_HOURS_PER_SHIFT,
    DEFAULT_WAGE_PREMIUMS,
    DEFAULT_WEEKEND_DAYS,
    _LOCK,
    _load_unlocked,
    _now_iso,
    _normalize_display_name,
    _save_unlocked,
    display_labels_for_store,
    hours_per_shift_for_store,
    staff_profiles_for_store,
    stores_path,
)

# ---- defaults / constants ----
DEFAULT_OT_MULTIPLIER = 1.25
DEFAULT_NIGHT_HOURS_PER_WEEKEND_SHIFT = 2.0
DEFAULT_WEEKS_PER_MONTH = 4.345  # 365.25/7/12
STATUTORY_WEEKLY_HOURS = 40.0
METHOD_LABEL = "classical_payroll_poc"
METHOD_NOTE = (
    "古典給与計算PoC（予定／実績）。銀行振込・支払いは行いません。"
    "組表本体は classical_greedy_heuristic。"
)

_HERE = Path(__file__).resolve().parent
_CSV_DIR = Path("/workspace/payroll_exports")


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month_token(token: str | None, *, now: datetime | None = None) -> tuple[int, int] | None:
    """'今月' / '9月' / '2026-09' / '9' → (year, month)。"""
    now = now or datetime.now().astimezone()
    raw = (token or "").strip()
    if not raw or raw in {"今月", "当月", "この月"}:
        return now.year, now.month
    m = re.match(r"^(?:(?P<y>20\d{2})[-/年])?(?P<m>1[0-2]|0?[1-9])\s*月?$", raw)
    if not m:
        return None
    month = int(m.group("m"))
    year = int(m.group("y")) if m.group("y") else now.year
    if month < 1 or month > 12:
        return None
    return year, month


def month_label(year: int, month: int) -> str:
    return f"{year}年{month}月"


def resolve_night_wage(member_or_profile: dict[str, Any], premiums: dict[str, float] | None = None) -> int:
    explicit = member_or_profile.get("night_hourly_wage")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    base = int(member_or_profile.get("hourly_wage") or DEFAULT_HOURLY_WAGE)
    prem = dict(DEFAULT_WAGE_PREMIUMS)
    if premiums:
        prem.update(premiums)
    return int(round(base * float(prem.get("night") or 1.25)))


def resolve_ot_wage(member_or_profile: dict[str, Any]) -> int:
    explicit = member_or_profile.get("overtime_hourly_wage")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    base = int(member_or_profile.get("hourly_wage") or DEFAULT_HOURLY_WAGE)
    return int(round(base * DEFAULT_OT_MULTIPLIER))


def _allowances_of(member_or_profile: dict[str, Any]) -> list[dict[str, Any]]:
    raw = member_or_profile.get("allowances") or []
    out = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip() or "手当"
        try:
            amount = int(a.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        typ = str(a.get("type") or "monthly").strip().lower()
        if typ not in {"monthly", "per_shift"}:
            typ = "monthly"
        out.append({"name": name, "amount": amount, "type": typ})
    return out


def member_payroll_fields(member: dict[str, Any], store: dict[str, Any] | None = None) -> dict[str, Any]:
    premiums = {}
    if store and isinstance(store.get("wage_premiums"), dict):
        premiums = {k: float(v) for k, v in store["wage_premiums"].items() if isinstance(v, (int, float))}
    wage = int(member.get("hourly_wage") or DEFAULT_HOURLY_WAGE)
    return {
        "hourly_wage": wage,
        "night_hourly_wage": resolve_night_wage(member, premiums),
        "overtime_hourly_wage": resolve_ot_wage(member),
        "commute_allowance": int(member.get("commute_allowance") or 0),
        "allowances": _allowances_of(member),
        "night_hourly_wage_explicit": member.get("night_hourly_wage") is not None,
        "overtime_hourly_wage_explicit": member.get("overtime_hourly_wage") is not None,
    }


def enrich_profiles_for_payroll(store: dict[str, Any] | None, scenario: dict[str, Any] | None = None) -> dict[str, dict]:
    """worker_id → 給与計算用プロファイル（交通費・深夜・残業・手当込み）。"""
    profiles = staff_profiles_for_store(store) if store else {}
    workers = list((scenario or {}).get("workers") or [])
    for w in workers:
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
    premiums = dict(DEFAULT_WAGE_PREMIUMS)
    if store and isinstance(store.get("wage_premiums"), dict):
        premiums.update(
            {k: float(v) for k, v in store["wage_premiums"].items() if isinstance(v, (int, float))}
        )
    # overlay member payroll fields
    if store:
        for m in store.get("members") or []:
            wid = str(m.get("worker_id") or "").strip()
            if not wid:
                continue
            base = profiles.setdefault(wid, {})
            fields = member_payroll_fields(m, store)
            base.update(fields)
            base["display_name"] = (
                _normalize_display_name(m.get("display_name") or m.get("worker_alias")) or wid
            )
            base["user_id"] = m.get("user_id")
            base["role"] = m.get("role")
            base["is_manager"] = bool(m.get("is_manager"))
    # fill defaults for any remaining
    for wid, p in profiles.items():
        p.setdefault("hourly_wage", DEFAULT_HOURLY_WAGE)
        p.setdefault("night_hourly_wage", resolve_night_wage(p, premiums))
        p.setdefault("overtime_hourly_wage", resolve_ot_wage(p))
        p.setdefault("commute_allowance", 0)
        p.setdefault("allowances", [])
    return profiles


def calculate_pay(
    *,
    hours: float,
    night_hours: float = 0.0,
    ot_hours: float = 0.0,
    work_days: int = 0,
    hourly_wage: int,
    night_hourly_wage: int,
    overtime_hourly_wage: int,
    commute_allowance: int = 0,
    allowances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """実績／予定時間から給与内訳を算出。根拠付き。"""
    hours = max(0.0, float(hours))
    night_hours = max(0.0, float(night_hours))
    ot_hours = max(0.0, float(ot_hours))
    # 深夜・残業は重複計上しないよう、基本時間から差し引き
    regular = max(0.0, hours - night_hours - ot_hours)
    # もし night+ot > hours なら比例圧縮
    if night_hours + ot_hours > hours + 1e-9 and hours > 0:
        scale = hours / (night_hours + ot_hours)
        night_hours *= scale
        ot_hours *= scale
        regular = 0.0
    work_days = max(0, int(work_days))
    allowances = allowances or []

    base_pay = hourly_wage * regular
    night_pay = night_hourly_wage * night_hours
    ot_pay = overtime_hourly_wage * ot_hours
    commute = commute_allowance * work_days
    allowance_rows = []
    allowance_total = 0.0
    for a in allowances:
        name = str(a.get("name") or "手当")
        amount = int(a.get("amount") or 0)
        typ = str(a.get("type") or "monthly")
        if typ == "per_shift":
            sub = amount * work_days
        else:
            sub = amount
        allowance_rows.append({"name": name, "type": typ, "unit": amount, "subtotal": int(round(sub))})
        allowance_total += sub

    total = base_pay + night_pay + ot_pay + commute + allowance_total
    formula = (
        f"基本({hourly_wage}×{regular:g}h) + 深夜({night_hourly_wage}×{night_hours:g}h) + "
        f"残業({overtime_hourly_wage}×{ot_hours:g}h) + 交通費({commute_allowance}×{work_days}日) + "
        f"手当({int(round(allowance_total))}円)"
    )
    return {
        "hours": round(hours, 2),
        "regular_hours": round(regular, 2),
        "night_hours": round(night_hours, 2),
        "ot_hours": round(ot_hours, 2),
        "work_days": work_days,
        "base_pay": int(round(base_pay)),
        "night_pay": int(round(night_pay)),
        "ot_pay": int(round(ot_pay)),
        "commute": int(round(commute)),
        "allowance_total": int(round(allowance_total)),
        "allowance_rows": allowance_rows,
        "total": int(round(total)),
        "rates": {
            "hourly_wage": int(hourly_wage),
            "night_hourly_wage": int(night_hourly_wage),
            "overtime_hourly_wage": int(overtime_hourly_wage),
            "commute_allowance": int(commute_allowance),
        },
        "formula": formula,
        "method": METHOD_LABEL,
        "method_note": METHOD_NOTE,
    }


def estimate_shift_labor_components(
    schedule: dict[str, list[bool]],
    scenario: dict[str, Any],
    store: dict[str, Any] | None = None,
    *,
    profiles: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """週次シフトから総人件費シミュレーション（通勤・深夜見込・残業見込込み）。

    日単位ソルバ向け。給与確定ではない。
    """
    profiles = profiles or enrich_profiles_for_payroll(store, scenario)
    hps = hours_per_shift_for_store(store)
    premiums = dict(DEFAULT_WAGE_PREMIUMS)
    if store and isinstance(store.get("wage_premiums"), dict):
        premiums.update(
            {k: float(v) for k, v in store["wage_premiums"].items() if isinstance(v, (int, float))}
        )
    weekend = list(store.get("weekend_days") or DEFAULT_WEEKEND_DAYS) if store else list(DEFAULT_WEEKEND_DAYS)
    holidays = list(store.get("holiday_days") or []) if store else []
    night_per_weekend = float(
        (store or {}).get("night_hours_per_weekend_shift") or DEFAULT_NIGHT_HOURS_PER_WEEKEND_SHIFT
    )

    workers = list(scenario.get("workers") or [])
    days = list(scenario.get("days") or [])
    per_staff: list[dict[str, Any]] = []
    total_cost = 0.0
    total_hours = 0.0
    total_commute = 0.0
    total_night_prem = 0.0
    total_ot_prem = 0.0
    total_allow = 0.0

    for w in workers:
        p = profiles.get(w) or {}
        wage = int(p.get("hourly_wage") or DEFAULT_HOURLY_WAGE)
        night_w = int(p.get("night_hourly_wage") or resolve_night_wage(p, premiums))
        ot_w = int(p.get("overtime_hourly_wage") or resolve_ot_wage(p))
        commute_u = int(p.get("commute_allowance") or 0)
        allows = p.get("allowances") or []
        bits = schedule.get(w) or [False] * len(days)
        work_days = sum(1 for b in bits if b)
        hours = work_days * hps
        night_hours = 0.0
        day_base = 0.0
        for d_idx, day in enumerate(days):
            if not bits[d_idx]:
                continue
            mult = 1.0
            if day in holidays:
                mult = max(mult, float(premiums.get("holiday") or 1.0))
            if day in weekend:
                mult = max(mult, float(premiums.get("weekend") or 1.0))
                night_hours += min(night_per_weekend, hps)
            day_base += wage * hps * mult
        ot_hours = max(0.0, hours - STATUTORY_WEEKLY_HOURS)
        # 深夜分は基本から差し引いて差額を加算（割増日の基本は day_base に既に含む）
        # シミュレーション総額 = 日割増込み基本 + 深夜差額 + 残業差額 + 交通 + 手当
        night_diff = max(0.0, night_w - wage) * night_hours
        # OT: 週40超の時間を残業時給で再評価（簡易: 基本時給分は day_base に含まれるので差額のみ）
        ot_diff = max(0.0, ot_w - wage) * ot_hours
        commute = commute_u * work_days
        allow_sum = 0.0
        for a in allows:
            amount = int(a.get("amount") or 0)
            typ = str(a.get("type") or "monthly")
            # 週次シミュレーションでは monthly を週按分
            if typ == "per_shift":
                allow_sum += amount * work_days
            else:
                allow_sum += amount / DEFAULT_WEEKS_PER_MONTH
        staff_total = day_base + night_diff + ot_diff + commute + allow_sum
        total_cost += staff_total
        total_hours += hours
        total_commute += commute
        total_night_prem += night_diff
        total_ot_prem += ot_diff
        total_allow += allow_sum
        per_staff.append(
            {
                "worker": w,
                "display_name": p.get("display_name") or w,
                "shifts": work_days,
                "hours": round(hours, 1),
                "night_hours_est": round(night_hours, 1),
                "ot_hours_est": round(ot_hours, 1),
                "base_with_day_premium": int(round(day_base)),
                "night_premium_yen": int(round(night_diff)),
                "ot_premium_yen": int(round(ot_diff)),
                "commute": int(round(commute)),
                "allowances_prorated": int(round(allow_sum)),
                "projected_cost": int(round(staff_total)),
                "hourly_wage": wage,
            }
        )

    return {
        "projected_labor_cost": int(round(total_cost)),
        "total_hours": round(total_hours, 1),
        "commute_total": int(round(total_commute)),
        "night_premium_total": int(round(total_night_prem)),
        "ot_premium_total": int(round(total_ot_prem)),
        "allowance_total": int(round(total_allow)),
        "cost_breakdown": per_staff,
        "includes": ["base+day_premium", "night_estimate", "ot_estimate", "commute", "allowances"],
        "method": "classical_labor_sim_with_premiums",
        "method_note": (
            "総人件費シミュレーション（通勤・深夜見込・残業見込・手当込み）。"
            "給与確定・振込ではありません。"
        ),
    }


def planned_hours_from_schedule(
    schedule: dict[str, list[bool]] | None,
    scenario: dict[str, Any],
    store: dict[str, Any] | None,
    worker_id: str,
    *,
    scale_weeks: float = 1.0,
) -> dict[str, float]:
    """1人分の予定時間・出勤日・深夜見込・残業見込。"""
    hps = hours_per_shift_for_store(store)
    days = list(scenario.get("days") or [])
    weekend = list(store.get("weekend_days") or DEFAULT_WEEKEND_DAYS) if store else list(DEFAULT_WEEKEND_DAYS)
    night_per = float(
        (store or {}).get("night_hours_per_weekend_shift") or DEFAULT_NIGHT_HOURS_PER_WEEKEND_SHIFT
    )
    bits = (schedule or {}).get(worker_id) or [False] * len(days)
    work_days = sum(1 for b in bits if b) * scale_weeks
    hours = work_days * hps
    night = 0.0
    for d_idx, day in enumerate(days):
        if bits[d_idx] and day in weekend:
            night += min(night_per, hps)
    night *= scale_weeks
    # 週単位残業をスケール
    week_hours = sum(1 for b in bits if b) * hps
    week_ot = max(0.0, week_hours - STATUTORY_WEEKLY_HOURS)
    ot = week_ot * scale_weeks
    return {
        "hours": round(hours, 2),
        "work_days": round(work_days, 2),
        "night_hours": round(night, 2),
        "ot_hours": round(ot, 2),
    }


def planned_hours_from_dated_shifts(
    store: dict[str, Any] | None, worker_id: str, *, year: int, month: int
) -> dict[str, float] | None:
    """条件付き自動作成で確定した日付シフト（store["dated_shifts"]）から、その月の予定を積算。

    深夜は 22:00〜翌5:00 の重なり、残業は週（ISO週）40時間超。該当月に日付シフトが無ければ None。
    """
    ds = (store or {}).get("dated_shifts") or {}
    prefix = f"{year:04d}-{month:02d}-"
    days = sorted(d for d in ds if d.startswith(prefix))
    if not days:
        return None
    from datetime import date as _date
    from shift_rules import hm_to_min, night_hours

    hours = night = 0.0
    work_days = 0
    week: dict[tuple[int, int], float] = {}
    for d in days:
        mine = [x for x in ds[d].get("slots") or [] if worker_id in (x.get("workers") or [])]
        if not mine:
            continue
        work_days += 1
        for x in mine:
            h = (hm_to_min(x["end"]) - hm_to_min(x["start"])) / 60.0
            hours += h
            night += night_hours(x["start"], x["end"])
            wk = _date.fromisoformat(d).isocalendar()[:2]
            week[wk] = week.get(wk, 0.0) + h
    ot = sum(max(0.0, h - STATUTORY_WEEKLY_HOURS) for h in week.values())
    return {
        "hours": round(hours, 2),
        "work_days": work_days,
        "night_hours": round(night, 2),
        "ot_hours": round(ot, 2),
        "dated_days": len(days),
    }


def _confirmed_schedule(store: dict[str, Any] | None) -> dict[str, list[bool]] | None:
    if not store:
        return None
    conf = store.get("confirmed_plan")
    if isinstance(conf, dict) and isinstance(conf.get("schedule"), dict):
        return conf["schedule"]
    return None


def _get_actual_override(store: dict[str, Any], year: int, month: int, worker_id: str) -> dict[str, Any] | None:
    key = _month_key(year, month)
    by_m = (store.get("actual_hours_by_month") or {}).get(key) or {}
    row = by_m.get(worker_id)
    return deepcopy(row) if isinstance(row, dict) else None


def build_staff_forecast(
    store: dict[str, Any],
    scenario: dict[str, Any],
    worker_id: str,
    *,
    year: int,
    month: int,
    scale_weeks: float | None = None,
) -> dict[str, Any] | None:
    """1スタッフの月次見込み（予定＋実績があれば実績給与）。"""
    profiles = enrich_profiles_for_payroll(store, scenario)
    p = profiles.get(worker_id)
    if not p:
        return None
    scale = DEFAULT_WEEKS_PER_MONTH if scale_weeks is None else float(scale_weeks)
    schedule = _confirmed_schedule(store)
    planned = planned_hours_from_schedule(schedule, scenario, store, worker_id, scale_weeks=scale)
    planned_basis = "weekly_x_month" if schedule is not None else "slot_estimate"
    dated = planned_hours_from_dated_shifts(store, worker_id, year=year, month=month)
    if dated is not None:
        planned = dated
        planned_basis = "dated_shifts"
    override = _get_actual_override(store, year, month, worker_id)
    if override:
        hours = float(override.get("actual_hours", planned["hours"]))
        night = float(override.get("night_hours", planned["night_hours"]))
        ot = float(override.get("ot_hours", planned["ot_hours"]))
        work_days = int(round(float(override.get("work_days", planned["work_days"]))))
        source = "actual"
    else:
        hours = planned["hours"]
        night = planned["night_hours"]
        ot = planned["ot_hours"]
        work_days = int(round(planned["work_days"]))
        source = "planned"

    pay = calculate_pay(
        hours=hours,
        night_hours=night,
        ot_hours=ot,
        work_days=work_days,
        hourly_wage=int(p["hourly_wage"]),
        night_hourly_wage=int(p["night_hourly_wage"]),
        overtime_hourly_wage=int(p["overtime_hourly_wage"]),
        commute_allowance=int(p.get("commute_allowance") or 0),
        allowances=p.get("allowances") or [],
    )
    planned_pay = calculate_pay(
        hours=planned["hours"],
        night_hours=planned["night_hours"],
        ot_hours=planned["ot_hours"],
        work_days=int(round(planned["work_days"])),
        hourly_wage=int(p["hourly_wage"]),
        night_hourly_wage=int(p["night_hourly_wage"]),
        overtime_hourly_wage=int(p["overtime_hourly_wage"]),
        commute_allowance=int(p.get("commute_allowance") or 0),
        allowances=p.get("allowances") or [],
    )
    return {
        "worker_id": worker_id,
        "display_name": p.get("display_name") or worker_id,
        "role": p.get("role"),
        "year": year,
        "month": month,
        "month_key": _month_key(year, month),
        "source": source,
        "actual_source": (override or {}).get("source") if override else None,
        "actual_meta": {k: (override or {}).get(k) for k in ("clock_days", "late_count", "early_count", "absent_count")} if override else None,
        "planned_basis": planned_basis,
        "has_confirmed_shift": schedule is not None or dated is not None,
        "scale_weeks": scale,
        "planned": {
            "hours": planned["hours"],
            "work_days": planned["work_days"],
            "night_hours": planned["night_hours"],
            "ot_hours": planned["ot_hours"],
            "pay": planned_pay,
        },
        "current": pay,  # 実績があれば実績、なければ予定
        "rates": pay["rates"],
        "formula": pay["formula"],
        "method": METHOD_LABEL,
    }


def month_forecast(store: dict[str, Any], scenario: dict[str, Any], *, year: int, month: int) -> dict[str, Any]:
    """全スタッフの月次見込み + 予算比較。"""
    workers = []
    for m in store.get("members") or []:
        wid = str(m.get("worker_id") or "").strip()
        if wid:
            workers.append(wid)
    # シナリオ枠も拾う
    for w in scenario.get("workers") or []:
        if w not in workers:
            workers.append(w)

    staff_rows = []
    for wid in workers:
        row = build_staff_forecast(store, scenario, wid, year=year, month=month)
        if row:
            staff_rows.append(row)

    total_planned = sum(int((r["planned"]["pay"]["total"])) for r in staff_rows)
    total_current = sum(int(r["current"]["total"]) for r in staff_rows)
    budget = store.get("labor_budget_monthly")
    try:
        budget_i = int(budget) if budget is not None else None
    except (TypeError, ValueError):
        budget_i = None
    diff = (budget_i - total_current) if budget_i is not None else None
    # 人件費率: 予算があるときは 予定/予算、なければ None（売上未連携）
    labor_rate = None
    if budget_i and budget_i > 0:
        labor_rate = round(100.0 * total_current / budget_i, 1)

    locked = False
    locked_rec = None
    key = _month_key(year, month)
    pm = (store.get("payroll_months") or {}).get(key)
    if isinstance(pm, dict) and pm.get("locked"):
        locked = True
        locked_rec = pm

    return {
        "year": year,
        "month": month,
        "month_key": key,
        "label": month_label(year, month),
        "staff": staff_rows,
        "total_planned_pay": total_planned,
        "total_current_pay": total_current,
        "labor_budget_monthly": budget_i,
        "budget_diff": diff,
        "labor_rate_pct": labor_rate,
        "locked": locked,
        "locked_record": locked_rec,
        "method": METHOD_LABEL,
        "method_note": METHOD_NOTE,
        "has_confirmed_shift": _confirmed_schedule(store) is not None or any(
            str(d).startswith(f"{year:04d}-{month:02d}-") for d in (store.get("dated_shifts") or {})
        ),
    }


def _source_label(row: dict[str, Any], *, short: bool = False) -> str:
    if row.get("source") == "actual":
        meta = row.get("actual_meta") or {}
        if row.get("actual_source") == "clock":
            if short:
                return f"実績・打刻{meta.get('clock_days') or 0}日"
            return (f"実績ベース（出勤・退勤の打刻 {meta.get('clock_days') or 0}日分／"
                    f"遅刻{meta.get('late_count') or 0}・早退{meta.get('early_count') or 0}・欠勤{meta.get('absent_count') or 0}）")
        return "実績" if short else "実績ベース（手入力）"
    basis = row.get("planned_basis")
    if basis == "dated_shifts":
        return "見込み・確定シフト" if short else "見込み（確定シフトの日付・時間から自動計算）"
    if basis == "weekly_x_month":
        return "予定" if short else "予定ベース（確定シフト×月換算）"
    return "予定" if short else "予定ベース（未確定のため枠ごとの試算）"


def format_month_payroll_text(forecast: dict[str, Any], *, store_name: str | None = None) -> str:
    title = f"{forecast['label']}の給与見込み"
    if store_name:
        title = f"「{store_name}」{title}"
    lines = [f"【{title}】"]
    if not forecast.get("has_confirmed_shift"):
        lines.append("※ 確定シフトが無いため、枠ごとの試算です。先に「確定」してください。")
    budget = forecast.get("labor_budget_monthly")
    cur = int(forecast.get("total_current_pay") or 0)
    planned = int(forecast.get("total_planned_pay") or 0)
    if budget is not None:
        diff = forecast.get("budget_diff")
        rate = forecast.get("labor_rate_pct")
        diff_s = f"{diff:+,}円" if diff is not None else "—"
        lines.append(f"今月の人件費予算: {int(budget):,}円")
        lines.append(f"現在の予定人件費: {cur:,}円")
        lines.append(f"予算との差額: {diff_s}")
        lines.append(f"人件費率（対予算）: {rate}%" if rate is not None else "人件費率: —")
    else:
        lines.append("今月の人件費予算: 未設定（「人件費予算 200000」で設定）")
        lines.append(f"現在の予定人件費: {cur:,}円（予定合計 {planned:,}円）")
    if forecast.get("locked"):
        lines.append("🔒 この月は給与確定済みです。")
    lines.append("")
    lines.append("■ スタッフ別")
    for r in forecast.get("staff") or []:
        pay = r["current"]
        src = _source_label(r, short=True)
        lines.append(
            f"  {r['display_name']}: {pay['hours']:g}h / "
            f"基本{pay['base_pay']:,} + 深夜{pay['night_pay']:,} + 残業{pay['ot_pay']:,} + "
            f"交通{pay['commute']:,} + 手当{pay['allowance_total']:,} = {pay['total']:,}円（{src}）"
        )
    lines.append("")
    lines.append(f"合計: {cur:,}円")
    lines.append(f"※ {METHOD_NOTE}")
    return "\n".join(lines)


def format_staff_payroll_text(row: dict[str, Any], *, store_name: str | None = None) -> str:
    pay = row["current"]
    rates = row["rates"]
    src = _source_label(row)
    title = f"{row['display_name']} の給与見込み（{month_label(row['year'], row['month'])}）"
    if store_name:
        title = f"「{store_name}」{title}"
    lines = [
        f"【{title}】",
        f"出典: {src}",
        f"勤務時間: {pay['hours']:g}h（基本{pay['regular_hours']:g} / 深夜{pay['night_hours']:g} / 残業{pay['ot_hours']:g}）",
        f"出勤日数: {pay['work_days']}日",
        f"基本給相当: {pay['base_pay']:,}円（時給{rates['hourly_wage']}円）",
        f"深夜: {pay['night_pay']:,}円（深夜時給{rates['night_hourly_wage']}円）",
        f"残業: {pay['ot_pay']:,}円（残業時給{rates['overtime_hourly_wage']}円）",
        f"交通費: {pay['commute']:,}円（{rates['commute_allowance']}円×{pay['work_days']}日）",
        f"手当: {pay['allowance_total']:,}円",
    ]
    for a in pay.get("allowance_rows") or []:
        lines.append(f"  ・{a['name']}（{a['type']}）: {a['subtotal']:,}円")
    lines.append(f"合計予定給与: {pay['total']:,}円")
    if row["source"] == "actual":
        pp = (row.get("planned") or {}).get("pay") or {}
        lines.append(f"（参考）確定シフトからの見込み: {int(pp.get('total') or 0):,}円 / {float((row.get('planned') or {}).get('hours') or 0):g}h")
    lines.append(f"計算式: {pay['formula']}")
    lines.append(f"※ {METHOD_NOTE}")
    return "\n".join(lines)


def format_payslip_text(record: dict[str, Any], *, store_name: str | None = None) -> str:
    """給与明細（ロック済み or 現在見込みの payslip 風）。"""
    name = record.get("display_name") or record.get("worker_id") or "?"
    mk = record.get("month_key") or ""
    title = f"給与明細 — {name}（{mk}）"
    if store_name:
        title = f"「{store_name}」{title}"
    pay = record.get("pay") or record.get("current") or record
    rates = pay.get("rates") or record.get("rates") or {}
    lines = [
        f"【{title}】",
        f"方法: {pay.get('method') or METHOD_LABEL}",
        f"勤務時間: {pay.get('hours', 0):g}h "
        f"（基本{pay.get('regular_hours', 0):g} / 深夜{pay.get('night_hours', 0):g} / 残業{pay.get('ot_hours', 0):g}）",
        f"出勤日数: {pay.get('work_days', 0)}日",
        "-----",
        f"基本: {pay.get('base_pay', 0):,}円",
        f"深夜: {pay.get('night_pay', 0):,}円",
        f"残業: {pay.get('ot_pay', 0):,}円",
        f"交通費: {pay.get('commute', 0):,}円",
        f"手当: {pay.get('allowance_total', 0):,}円",
    ]
    for a in pay.get("allowance_rows") or []:
        lines.append(f"  ・{a['name']}: {a['subtotal']:,}円")
    lines.append("-----")
    lines.append(f"支給合計: {pay.get('total', 0):,}円")
    lines.append(f"時給 {rates.get('hourly_wage', '—')} / 深夜 {rates.get('night_hourly_wage', '—')} / "
                 f"残業 {rates.get('overtime_hourly_wage', '—')}")
    if pay.get("formula"):
        lines.append(f"根拠: {pay['formula']}")
    lines.append("※ 明細表示のみ。振込・支払いは行いません。")
    lines.append(f"※ {METHOD_NOTE}")
    return "\n".join(lines)


def format_budget_status_text(forecast: dict[str, Any], *, store_name: str | None = None) -> str:
    """人件費予算ダッシュボード短文。"""
    head = f"【人件費予算 — {forecast['label']}】"
    if store_name:
        head = f"「{store_name}」{head}"
    budget = forecast.get("labor_budget_monthly")
    cur = int(forecast.get("total_current_pay") or 0)
    lines = [head]
    if budget is None:
        lines.append("予算未設定です。「人件費予算 200000」と送ってください。")
        lines.append(f"現在の予定人件費: {cur:,}円")
    else:
        diff = forecast.get("budget_diff")
        rate = forecast.get("labor_rate_pct")
        lines.append(f"今月の人件費予算: {int(budget):,}円")
        lines.append(f"現在の予定人件費: {cur:,}円")
        lines.append(f"予算との差額: {diff:+,}円" if diff is not None else "予算との差額: —")
        lines.append(f"人件費率（対予算）: {rate}%" if rate is not None else "人件費率: —")
    lines.append(f"※ {METHOD_NOTE}")
    return "\n".join(lines)


# ---- persistence helpers (stores.json) ----

def set_labor_budget(store_id: str, budget_yen: int) -> tuple[bool, str, dict[str, Any] | None]:
    if budget_yen < 0 or budget_yen > 100_000_000:
        return False, "人件費予算は 0〜1億の範囲で指定してください。", None
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return False, "店舗が見つかりません。", None
        store["labor_budget_monthly"] = int(budget_yen)
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        return True, f"人件費予算を {budget_yen:,}円 に設定しました。", deepcopy(store)


def set_member_payroll_fields(
    store_id: str,
    *,
    worker_id: str | None = None,
    display_name: str | None = None,
    commute_allowance: int | None = None,
    night_hourly_wage: int | None = None,
    overtime_hourly_wage: int | None = None,
    add_allowance: dict[str, Any] | None = None,
    clear_allowances: bool = False,
) -> tuple[bool, str, dict[str, Any] | None]:
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return False, "店舗が見つかりません。", None
        found = None
        dn = _normalize_display_name(display_name)
        for m in store.get("members") or []:
            if worker_id and str(m.get("worker_id") or "") == worker_id:
                found = m
                break
            if dn:
                mdn = _normalize_display_name(m.get("display_name") or m.get("worker_alias"))
                if mdn == dn:
                    found = m
                    break
        if found is None:
            return False, "対象スタッフが見つかりません。", None
        if commute_allowance is not None:
            if commute_allowance < 0 or commute_allowance > 50_000:
                return False, "交通費は 0〜50000 円/日の範囲で指定してください。", None
            found["commute_allowance"] = int(commute_allowance)
        if night_hourly_wage is not None:
            if night_hourly_wage < 0 or night_hourly_wage > 100_000:
                return False, "深夜時給は 0〜100000 円の範囲で指定してください。", None
            found["night_hourly_wage"] = int(night_hourly_wage)
        if overtime_hourly_wage is not None:
            if overtime_hourly_wage < 0 or overtime_hourly_wage > 100_000:
                return False, "残業時給は 0〜100000 円の範囲で指定してください。", None
            found["overtime_hourly_wage"] = int(overtime_hourly_wage)
        if clear_allowances:
            found["allowances"] = []
        if add_allowance:
            allows = list(found.get("allowances") or [])
            name = str(add_allowance.get("name") or "手当").strip()
            amount = int(add_allowance.get("amount") or 0)
            typ = str(add_allowance.get("type") or "monthly")
            # replace same name
            allows = [a for a in allows if str(a.get("name")) != name]
            allows.append({"name": name, "amount": amount, "type": typ})
            found["allowances"] = allows
        # ensure defaults exist
        found.setdefault("commute_allowance", 0)
        found.setdefault("allowances", [])
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        label = found.get("display_name") or found.get("worker_id") or "?"
        fields = member_payroll_fields(found, store)
        msg = (
            f"{label} の給与設定を更新しました。\n"
            f"時給{fields['hourly_wage']} / 深夜{fields['night_hourly_wage']} / "
            f"残業{fields['overtime_hourly_wage']} / 交通費{fields['commute_allowance']}円/日 / "
            f"手当{len(fields['allowances'])}件"
        )
        return True, msg, deepcopy(store)


def set_actual_hours(
    store_id: str,
    *,
    year: int,
    month: int,
    worker_id: str | None = None,
    display_name: str | None = None,
    actual_hours: float | None = None,
    night_hours: float | None = None,
    ot_hours: float | None = None,
    work_days: int | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    key = _month_key(year, month)
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return False, "店舗が見つかりません。", None
        pm = (store.get("payroll_months") or {}).get(key)
        if isinstance(pm, dict) and pm.get("locked"):
            return False, f"{month_label(year, month)}は給与確定済みのため実績を変更できません。", None
        found = None
        dn = _normalize_display_name(display_name)
        for m in store.get("members") or []:
            if worker_id and str(m.get("worker_id") or "") == worker_id:
                found = m
                break
            if dn:
                mdn = _normalize_display_name(m.get("display_name") or m.get("worker_alias"))
                if mdn == dn:
                    found = m
                    break
        if found is None:
            return False, "対象スタッフが見つかりません。", None
        wid = str(found.get("worker_id") or "")
        by_m = store.setdefault("actual_hours_by_month", {})
        month_map = by_m.setdefault(key, {})
        row = dict(month_map.get(wid) or {})
        if actual_hours is not None:
            if actual_hours < 0 or actual_hours > 744:
                return False, "実績時間は 0〜744 の範囲で指定してください。", None
            row["actual_hours"] = float(actual_hours)
        if night_hours is not None:
            row["night_hours"] = float(max(0.0, night_hours))
        if ot_hours is not None:
            row["ot_hours"] = float(max(0.0, ot_hours))
        if work_days is not None:
            row["work_days"] = int(max(0, work_days))
        row["updated_at"] = _now_iso()
        row["source"] = "manual"  # 手入力は打刻集計で上書きしない
        month_map[wid] = row
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        label = found.get("display_name") or wid
        msg = (
            f"{label} の{month_label(year, month)}実績を更新しました。\n"
            f"時間={row.get('actual_hours', '—')} / 深夜={row.get('night_hours', '—')} / "
            f"残業={row.get('ot_hours', '—')} / 出勤日={row.get('work_days', '—')}"
        )
        return True, msg, deepcopy(store)


def lock_month_payroll(
    store: dict[str, Any],
    scenario: dict[str, Any],
    *,
    year: int,
    month: int,
    locked_by: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """実績（なければ予定）で月次給与をロック保存。振込はしない。"""
    key = _month_key(year, month)
    store_id = store["store_id"]
    forecast = month_forecast(store, scenario, year=year, month=month)
    if forecast.get("locked"):
        return False, f"{month_label(year, month)}は既に給与確定済みです。", None

    staff_payload = {}
    for r in forecast["staff"]:
        pay = r["current"]
        staff_payload[r["worker_id"]] = {
            "worker_id": r["worker_id"],
            "display_name": r["display_name"],
            "role": r.get("role"),
            "source": r["source"],
            "pay": pay,
            "planned": r["planned"],
            "rates": r["rates"],
            "formula": r["formula"],
            "method": METHOD_LABEL,
        }
    record = {
        "locked": True,
        "locked_at": _now_iso(),
        "locked_by": locked_by,
        "month_key": key,
        "year": year,
        "month": month,
        "labor_budget_monthly": forecast.get("labor_budget_monthly"),
        "totals": {
            "total_pay": forecast["total_current_pay"],
            "total_planned_pay": forecast["total_planned_pay"],
            "staff_count": len(staff_payload),
        },
        "staff": staff_payload,
        "method": METHOD_LABEL,
        "method_note": METHOD_NOTE,
        "no_bank_transfer": True,
    }
    with _LOCK:
        db = _load_unlocked()
        s = db["stores"].get(store_id)
        if not s:
            return False, "店舗が見つかりません。", None
        months = s.setdefault("payroll_months", {})
        if isinstance(months.get(key), dict) and months[key].get("locked"):
            return False, f"{month_label(year, month)}は既に給与確定済みです。", None
        months[key] = record
        s["updated_at"] = _now_iso()
        _save_unlocked(db)
    msg = (
        f"{month_label(year, month)}の給与を確定・保存しました。\n"
        f"スタッフ {len(staff_payload)}名 / 合計 {forecast['total_current_pay']:,}円\n"
        f"※ 計算データのロックです。振込・支払いは行いません。\n"
        f"明細: 「給与明細 太郎 {month}月」 / CSV: 「給与CSV {month}月」"
    )
    return True, msg, record


def get_locked_payslip(store: dict[str, Any], worker_id: str, *, year: int, month: int) -> dict[str, Any] | None:
    key = _month_key(year, month)
    pm = (store.get("payroll_months") or {}).get(key)
    if not isinstance(pm, dict):
        return None
    staff = pm.get("staff") or {}
    row = staff.get(worker_id)
    if not row:
        return None
    out = deepcopy(row)
    out["month_key"] = key
    out["locked"] = bool(pm.get("locked"))
    out["locked_at"] = pm.get("locked_at")
    return out


def find_worker_id(store: dict[str, Any], who: str | None) -> str | None:
    if not who:
        return None
    who = who.strip()
    labels = display_labels_for_store(store)
    profiles = staff_profiles_for_store(store)
    if who in profiles:
        return who
    for wid, lab in labels.items():
        if lab == who:
            return wid
    # さん付き除去済み想定; 部分一致はしない
    dn = _normalize_display_name(who)
    for m in store.get("members") or []:
        mdn = _normalize_display_name(m.get("display_name") or m.get("worker_alias"))
        if mdn and dn and mdn == dn:
            return str(m.get("worker_id") or "") or None
    return None


def export_payroll_csv(
    store: dict[str, Any],
    scenario: dict[str, Any],
    *,
    year: int,
    month: int,
    out_dir: Path | None = None,
) -> tuple[bool, str, Path | None]:
    """月次給与CSVを /workspace 配下に書き出し。"""
    key = _month_key(year, month)
    out_dir = out_dir or _CSV_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]+", "_", store.get("store_name") or store.get("store_id") or "store")
    path = out_dir / f"payroll_{safe_name}_{key}.csv"

    locked = (store.get("payroll_months") or {}).get(key)
    if isinstance(locked, dict) and locked.get("staff"):
        rows_src = []
        for wid, rec in locked["staff"].items():
            pay = rec.get("pay") or {}
            rows_src.append((wid, rec.get("display_name") or wid, pay, rec.get("source") or "locked"))
    else:
        forecast = month_forecast(store, scenario, year=year, month=month)
        rows_src = []
        for r in forecast["staff"]:
            rows_src.append((r["worker_id"], r["display_name"], r["current"], r["source"]))

    fieldnames = [
        "month", "worker_id", "display_name", "source",
        "hours", "regular_hours", "night_hours", "ot_hours", "work_days",
        "hourly_wage", "night_hourly_wage", "overtime_hourly_wage", "commute_allowance",
        "base_pay", "night_pay", "ot_pay", "commute", "allowance_total", "total",
        "formula", "method",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for wid, name, pay, source in rows_src:
            rates = pay.get("rates") or {}
            w.writerow(
                {
                    "month": key,
                    "worker_id": wid,
                    "display_name": name,
                    "source": source,
                    "hours": pay.get("hours"),
                    "regular_hours": pay.get("regular_hours"),
                    "night_hours": pay.get("night_hours"),
                    "ot_hours": pay.get("ot_hours"),
                    "work_days": pay.get("work_days"),
                    "hourly_wage": rates.get("hourly_wage"),
                    "night_hourly_wage": rates.get("night_hourly_wage"),
                    "overtime_hourly_wage": rates.get("overtime_hourly_wage"),
                    "commute_allowance": rates.get("commute_allowance"),
                    "base_pay": pay.get("base_pay"),
                    "night_pay": pay.get("night_pay"),
                    "ot_pay": pay.get("ot_pay"),
                    "commute": pay.get("commute"),
                    "allowance_total": pay.get("allowance_total"),
                    "total": pay.get("total"),
                    "formula": pay.get("formula"),
                    "method": pay.get("method") or METHOD_LABEL,
                }
            )
    msg = (
        f"{month_label(year, month)}の給与CSVを書き出しました。\n"
        f"パス: {path}\n"
        f"※ 計算データのみ。振込ファイルではありません。"
    )
    return True, msg, path


def backfill_member_payroll_defaults(store: dict[str, Any]) -> bool:
    """既存メンバーに給与フィールドが無ければ埋める。"""
    changed = False
    for m in store.get("members") or []:
        if "commute_allowance" not in m:
            m["commute_allowance"] = 0
            changed = True
        if "allowances" not in m:
            m["allowances"] = []
            changed = True
        if "night_hourly_wage" not in m:
            m["night_hourly_wage"] = None
            changed = True
        if "overtime_hourly_wage" not in m:
            m["overtime_hourly_wage"] = None
            changed = True
    if "labor_budget_monthly" not in store:
        store["labor_budget_monthly"] = None
        changed = True
    if "payroll_months" not in store:
        store["payroll_months"] = {}
        changed = True
    if "actual_hours_by_month" not in store:
        store["actual_hours_by_month"] = {}
        changed = True
    if "night_hours_per_weekend_shift" not in store:
        store["night_hours_per_weekend_shift"] = DEFAULT_NIGHT_HOURS_PER_WEEKEND_SHIFT
        changed = True
    return changed
