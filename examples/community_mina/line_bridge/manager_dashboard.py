#!/usr/bin/env python3
"""店長ダッシュボード — 本日の勤怠・欠員、今月の時間／人件費、希望未提出。

店舗隔離: 渡された store のみ参照。他店データは一切含めない。
FREE: 本日＋未提出の簡易版。STANDARD/PRO（payroll 機能）: 時間・人件費フル。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import attendance as att
import shift_rules as sr
from billing import has_feature
from line_ui import (
    encode_postback,
    flex_button_postback,
    manager_menu_items,
    qr_postback,
    with_quick_reply,
)
from payroll import month_forecast
from plans import POC_FOOTER
from shift_messages import POC_BRANDING_COPY


def _names(store: dict[str, Any], wids: list[str], *, limit: int = 6) -> str:
    if not wids:
        return "なし"
    labels = [sr.label_of(store, w) for w in wids]
    if len(labels) <= limit:
        return "、".join(labels)
    return "、".join(labels[:limit]) + f" 他{len(labels) - limit}名"


def today_snapshot(store: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """本日の出勤状況と枠ごとの充足（確定シフト基準）。"""
    d = (today or sr.now_jst().date()).isoformat()
    att.sweep_absences(store, now=sr.now_jst())
    day_att = (store.get("attendance") or {}).get(d) or {}
    day_shift = (store.get("dated_shifts") or {}).get(d) or {}
    slots_raw = list(day_shift.get("slots") or [])
    rules = sr.get_rules(store)
    # 確定シフトが無い日は条件の枠定義だけ見せる（割当0）
    if not slots_raw:
        slots_raw = [
            {"name": s["name"], "start": s["start"], "end": s["end"],
             "workers": [], "required": int(s.get("required") or 0)}
            for s in rules["slots"]
        ]
        from_confirmed = False
    else:
        from_confirmed = True

    working: list[str] = []
    done: list[str] = []
    absent: list[str] = []
    not_in: list[str] = []
    planned: set[str] = set()

    slot_rows = []
    for s in slots_raw:
        workers = list(s.get("workers") or [])
        planned.update(workers)
        if s.get("required") is not None:
            required = int(s["required"])
        else:
            required = next(
                (int(r.get("required") or 0) for r in rules["slots"] if r["name"] == s.get("name")),
                max(len(workers), 0),
            )
        # 欠勤者は充足から除外
        presentish = [w for w in workers if not (day_att.get(w) or {}).get("absent")]
        short = max(0, required - len(presentish))
        slot_rows.append({
            "name": s.get("name") or "?",
            "start": s.get("start"),
            "end": s.get("end"),
            "required": required,
            "assigned": len(workers),
            "effective": len(presentish),
            "short": short,
            "workers": workers,
        })

    # 打刻のみ（シフト外）も含める
    all_wids = planned | set(day_att.keys())
    for wid in sorted(all_wids, key=lambda w: sr.label_of(store, w)):
        rec = day_att.get(wid) or {}
        if rec.get("absent"):
            absent.append(wid)
        elif rec.get("in") and not rec.get("out"):
            working.append(wid)
        elif rec.get("in") and rec.get("out"):
            done.append(wid)
        elif wid in planned:
            not_in.append(wid)

    total_required = sum(s["required"] for s in slot_rows)
    total_short = sum(s["short"] for s in slot_rows)
    return {
        "date": d,
        "label": sr.date_label(d),
        "from_confirmed": from_confirmed,
        "working": working,
        "done": done,
        "absent": absent,
        "not_in": not_in,
        "slots": slot_rows,
        "total_required": total_required,
        "total_short": total_short,
        "planned_count": len(planned),
    }


def month_snapshot(
    store: dict[str, Any],
    scenario: dict[str, Any],
    *,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    now = sr.now_jst()
    y = year or now.year
    m = month or now.month
    # 打刻があれば実績を給与見込みに反映（手入力・ロック月は attendance 側で保護）
    att.recompute_month_actuals(store, y, m)
    fc = month_forecast(store, scenario, year=y, month=m)
    planned_h = sum(float((r.get("planned") or {}).get("hours") or 0) for r in fc.get("staff") or [])
    # 実績時間: source==actual の current 相当は hours in current pay; use override hours via planned vs current
    actual_h = 0.0
    actual_staff = 0
    for r in fc.get("staff") or []:
        if r.get("source") == "actual":
            actual_staff += 1
            # current pay is based on actual hours — recover from formula fields if present
            cur = r.get("current") or {}
            actual_h += float(cur.get("hours") or 0)
        else:
            # still count clock meta if any without full override? skip
            pass
    # Also sum from attendance month_actuals directly for honesty when forecast uses planned
    from attendance import month_actuals
    ma = month_actuals(store, y, m)
    clock_h = sum(float(v.get("actual_hours") or 0) for v in ma.values())
    clock_staff = sum(1 for v in ma.values() if float(v.get("actual_hours") or 0) > 0 or int(v.get("work_days") or 0) > 0)

    budget = fc.get("labor_budget_monthly")
    rules = sr.get_rules(store)
    cap = rules.get("cost_cap")
    cap_yen = int(cap["yen"]) if isinstance(cap, dict) and cap.get("yen") is not None else None
    cap_period = (cap or {}).get("period") if isinstance(cap, dict) else None
    # Prefer explicit labor_budget_monthly; else monthly cost_cap
    effective_budget = budget
    budget_source = "labor_budget"
    if effective_budget is None and cap_yen is not None and cap_period == "month":
        effective_budget = cap_yen
        budget_source = "cost_cap"
    elif effective_budget is None and cap_yen is not None and cap_period == "week":
        # show weekly cap separately; don't pretend it's monthly
        budget_source = "cost_cap_week"

    planned_pay = int(fc.get("total_planned_pay") or 0)
    # 実績人件費: only staff with actual source; else 0 / None
    actual_pay = sum(
        int((r.get("current") or {}).get("total") or 0)
        for r in (fc.get("staff") or [])
        if r.get("source") == "actual"
    )
    has_actual_pay = any(r.get("source") == "actual" for r in (fc.get("staff") or []))

    used = actual_pay if has_actual_pay else planned_pay
    diff = (int(effective_budget) - used) if effective_budget is not None else None
    rate = round(100.0 * used / effective_budget, 1) if effective_budget and effective_budget > 0 else None

    return {
        "year": y,
        "month": m,
        "label": fc.get("label") or f"{y}年{m}月",
        "planned_hours": round(planned_h, 1),
        "actual_hours": round(clock_h, 1) if clock_staff else (round(actual_h, 1) if actual_staff else None),
        "clock_staff": clock_staff,
        "planned_pay": planned_pay,
        "actual_pay": actual_pay if has_actual_pay else None,
        "has_actual_pay": has_actual_pay,
        "budget": effective_budget,
        "budget_source": budget_source,
        "week_cap_yen": cap_yen if cap_period == "week" else None,
        "budget_diff": diff,
        "labor_rate_pct": rate,
        "has_confirmed_shift": bool(fc.get("has_confirmed_shift")),
        "staff_count": len(fc.get("staff") or []),
    }


def prefs_snapshot(store: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    dates = sr.planning_dates(store, today=today)
    t = sr.tally(store, dates)
    return {
        "dates": dates,
        "period_label": f"{sr.date_label(dates[0])}〜{sr.date_label(dates[-1])}" if dates else "—",
        "submitted": list(t["submitted"]),
        "missing": list(t["missing"]),
        "submitted_n": len(t["submitted"]),
        "missing_n": len(t["missing"]),
        "total_n": len(t["submitted"]) + len(t["missing"]),
    }


def build_dashboard(
    store: dict[str, Any],
    scenario: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    full = has_feature(store, "payroll")
    return {
        "store_id": store.get("store_id"),
        "store_name": store.get("store_name") or "店舗",
        "full": full,
        "today": today_snapshot(store, today=today),
        "month": month_snapshot(store, scenario) if full else None,
        "prefs": prefs_snapshot(store, today=today),
        "plan_hint": None if full else "フリープランのため人件費・総労働時間は非表示です。「プラン」からスタンダード以上でご覧いただけます。",
    }


def _row(label: str, value: str, *, label_color: str = "#64748b", value_color: str = "#0f172a", bold: bool = False) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "xs", "color": label_color, "flex": 3, "wrap": True},
            {
                "type": "text",
                "text": value,
                "size": "xs",
                "color": value_color,
                "flex": 5,
                "wrap": True,
                "align": "end",
                **({"weight": "bold"} if bold else {}),
            },
        ],
        "margin": "sm",
    }


def _sep() -> dict[str, Any]:
    return {"type": "separator", "margin": "md"}


def _section(title: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": title,
        "size": "sm",
        "weight": "bold",
        "color": "#1e293b",
        "margin": "md",
    }


def build_dashboard_flex(data: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    today = data["today"]
    prefs = data["prefs"]
    body: list[dict[str, Any]] = []

    body.append(_section(f"本日 {today['label']}"))
    if not today["from_confirmed"] and today["planned_count"] == 0:
        body.append({
            "type": "text",
            "text": "本日の確定シフトはありません（条件の必要人数のみ表示）",
            "size": "xxs",
            "color": "#94a3b8",
            "wrap": True,
            "margin": "sm",
        })
    body.append(_row("出勤中", f"{len(today['working'])}名（{_names(store, today['working'])}）",
                     value_color="#16a34a" if today["working"] else "#64748b", bold=bool(today["working"])))
    body.append(_row("退勤済", f"{len(today['done'])}名（{_names(store, today['done'])}）"))
    body.append(_row("未出勤", f"{len(today['not_in'])}名（{_names(store, today['not_in'])}）",
                     value_color="#dc2626" if today["not_in"] else "#64748b"))
    body.append(_row("欠勤", f"{len(today['absent'])}名（{_names(store, today['absent'])}）",
                     value_color="#dc2626" if today["absent"] else "#64748b"))

    # vacancies per slot
    if today["slots"]:
        vac_parts = []
        for s in today["slots"]:
            mark = "⚠" if s["short"] else "✅"
            vac_parts.append(f"{s['name']} {s['effective']}/{s['required']}{mark}")
        body.append(_row(
            "枠の充足",
            "  ".join(vac_parts),
            value_color="#dc2626" if today["total_short"] else "#16a34a",
            bold=True,
        ))
        if today["total_short"]:
            body.append({
                "type": "text",
                "text": f"欠員合計 {today['total_short']}名（必要 {today['total_required']}・有効配置 {today['total_required'] - today['total_short']}）",
                "size": "xxs",
                "color": "#dc2626",
                "wrap": True,
                "margin": "sm",
            })

    body.append(_sep())
    body.append(_section(f"今月（{data['month']['label'] if data.get('month') else '—'}）" if data.get("full") else "今月"))
    if data.get("full") and data.get("month"):
        mo = data["month"]
        body.append(_row("総労働時間（見込み）", f"{mo['planned_hours']:g} 時間"))
        if mo["actual_hours"] is not None:
            body.append(_row("総労働時間（実績・打刻）", f"{mo['actual_hours']:g} 時間（{mo['clock_staff']}名分）", bold=True))
        else:
            body.append(_row("総労働時間（実績）", "まだ打刻なし"))
        body.append(_row("予定人件費", f"{mo['planned_pay']:,} 円", bold=True))
        if mo["has_actual_pay"]:
            body.append(_row("実績人件費", f"{mo['actual_pay']:,} 円", bold=True))
        else:
            body.append(_row("実績人件費", "—（打刻反映後に表示）"))
        if mo["budget"] is not None:
            body.append(_row("予算", f"{int(mo['budget']):,} 円"))
            if mo["budget_diff"] is not None:
                used_label = "実績" if mo["has_actual_pay"] else "予定"
                body.append(_row(
                    f"予算との差（{used_label}基準）",
                    f"{mo['budget_diff']:+,} 円" + (f"（消化 {mo['labor_rate_pct']}%）" if mo["labor_rate_pct"] is not None else ""),
                    value_color="#16a34a" if mo["budget_diff"] >= 0 else "#dc2626",
                    bold=True,
                ))
        elif mo.get("week_cap_yen") is not None:
            body.append(_row("週の人件費上限", f"{mo['week_cap_yen']:,} 円（条件）"))
        else:
            body.append(_row("予算", "未設定（「人件費予算 200000」）"))
        if not mo["has_confirmed_shift"]:
            body.append({
                "type": "text",
                "text": "※ 確定シフトが少ないため見込みは試算です",
                "size": "xxs",
                "color": "#94a3b8",
                "wrap": True,
                "margin": "sm",
            })
    else:
        body.append({
            "type": "text",
            "text": data.get("plan_hint") or "人件費・総労働時間はスタンダード以上で表示されます。",
            "size": "xs",
            "color": "#64748b",
            "wrap": True,
            "margin": "sm",
        })
        body.append(flex_button_postback(
            "料金プランを見る", encode_postback(action="show_plans"), style="primary", display_text="料金プラン",
        ))

    body.append(_sep())
    body.append(_section(f"シフト希望（{prefs['period_label']}）"))
    body.append(_row(
        "提出",
        f"{prefs['submitted_n']}/{prefs['total_n']}名",
        bold=True,
    ))
    body.append(_row(
        "未提出",
        f"{prefs['missing_n']}名（{_names(store, prefs['missing'])}）",
        value_color="#dc2626" if prefs["missing_n"] else "#16a34a",
        bold=bool(prefs["missing_n"]),
    ))

    body.append({
        "type": "text",
        "text": POC_FOOTER,
        "size": "xxs",
        "color": "#94a3b8",
        "wrap": True,
        "margin": "lg",
    })

    footer_btns = [
        flex_button_postback("提出状況", encode_postback(action="avail_tally"), style="secondary", display_text="提出状況"),
        flex_button_postback("未提出者にリマインド", encode_postback(action="avail_remind"), style="primary", display_text="未提出者にリマインド"),
        flex_button_postback("今日の勤怠", encode_postback(action="att_today"), style="secondary", display_text="今日の勤怠"),
    ]
    if data.get("full"):
        footer_btns.append(
            flex_button_postback("給与 今月", encode_postback(action="payroll_month", month="今月"), style="secondary", display_text="給与 今月")
        )
    footer_btns.append(
        flex_button_postback("条件で自動作成", encode_postback(action="rule_plans"), style="primary", display_text="条件でシフト作成")
    )

    title = f"店長ダッシュボード｜{data['store_name']}"
    return {
        "type": "flex",
        "altText": f"{title}｜{POC_BRANDING_COPY}",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "店長ダッシュボード", "weight": "bold", "size": "md", "color": "#0f172a"},
                    {"type": "text", "text": data["store_name"], "size": "xs", "color": "#64748b", "margin": "sm"},
                ],
                "paddingAll": "16px",
                "backgroundColor": "#f1f5f9",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": body,
                "paddingAll": "16px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": footer_btns,
                "paddingAll": "12px",
            },
        },
    }


def dashboard_quick_items(*, full: bool = True) -> list[dict[str, Any]]:
    items = [
        qr_postback("提出状況", encode_postback(action="avail_tally"), display_text="提出状況"),
        qr_postback("未提出リマインド", encode_postback(action="avail_remind"), display_text="未提出者にリマインド"),
        qr_postback("今日の勤怠", encode_postback(action="att_today"), display_text="今日の勤怠"),
        qr_postback("条件で自動作成", encode_postback(action="rule_plans"), display_text="条件でシフト作成"),
        qr_postback("メニュー", encode_postback(action="manager_menu"), display_text="メニュー"),
    ]
    if full:
        items.insert(3, qr_postback("給与 今月", encode_postback(action="payroll_month", month="今月"), display_text="給与 今月"))
    return (items + manager_menu_items())[:13]


def dashboard_messages(store: dict[str, Any], scenario: dict[str, Any], *, today: date | None = None) -> list[dict[str, Any]]:
    data = build_dashboard(store, scenario, today=today)
    flex = build_dashboard_flex(data, store)
    tip = {
        "type": "text",
        "text": (
            f"【{data['store_name']}】ダッシュボードです。\n"
            "本日の出勤・欠員、今月の時間／人件費、希望の未提出が一覧できます。"
            if data["full"]
            else f"【{data['store_name']}】ダッシュボード（簡易）です。\n人件費・総労働時間はスタンダード以上で表示されます。"
        ),
    }
    return [
        with_quick_reply(tip, dashboard_quick_items(full=data["full"])),
        with_quick_reply(flex, dashboard_quick_items(full=data["full"])),
    ]
