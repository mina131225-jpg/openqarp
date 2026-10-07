#!/usr/bin/env python3
"""Subscription plans + feature gates (store_id scoped).

LINE Mini App IAP is consumable-only — do NOT fake recurring IAP.
Paid plans use an abstracted Payment Provider + external checkout +
success webhook that attaches a plan to store_id.

On stop / expiry / payment failure: disable paid features but NEVER
delete store / staff / shift / payroll data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

TRIAL_DAYS = 14

PLAN_FREE = "FREE"
PLAN_STANDARD = "STANDARD"
PLAN_PRO = "PRO"
PLAN_ORDER = (PLAN_FREE, PLAN_STANDARD, PLAN_PRO)

STATUS_NONE = "none"
STATUS_TRIALING = "trialing"
STATUS_ACTIVE = "active"
STATUS_PAST_DUE = "past_due"
STATUS_CANCELED = "canceled"
STATUS_EXPIRED = "expired"

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    PLAN_FREE: {
        "code": PLAN_FREE,
        "label": "フリー",
        "price_yen_month": 0,
        "max_staff": 5,
        "max_stores_per_manager": 1,
        "features": {
            "basic_shift": True,
            "three_plans": False,
            "labor_opt": False,
            "payroll": False,
            "csv_export": False,
            "qaoa_compare": False,
            "advanced_labor": False,
            "multi_store": False,
        },
        "blurb": "1店舗・スタッフ5名・基本シフト",
    },
    PLAN_STANDARD: {
        "code": PLAN_STANDARD,
        "label": "スタンダード",
        "price_yen_month": 980,
        "max_staff": 20,
        "max_stores_per_manager": 1,
        "features": {
            "basic_shift": True,
            "three_plans": True,
            "labor_opt": True,
            "payroll": True,
            "csv_export": True,
            "qaoa_compare": False,
            "advanced_labor": False,
            "multi_store": False,
        },
        "blurb": "スタッフ20名・3案・人件費最適化・給与/CSV",
    },
    PLAN_PRO: {
        "code": PLAN_PRO,
        "label": "プロ",
        "price_yen_month": 2980,
        "max_staff": 200,
        "max_stores_per_manager": 20,
        "features": {
            "basic_shift": True,
            "three_plans": True,
            "labor_opt": True,
            "payroll": True,
            "csv_export": True,
            "qaoa_compare": True,
            "advanced_labor": True,
            "multi_store": True,
        },
        "blurb": "大規模・フル最適化・QAOA比較・複数店舗",
    },
}

FEATURE_LABELS_JA = {
    "basic_shift": "基本シフト（希望休・表示）",
    "three_plans": "シフト3案（希望/人件費/バランス）",
    "labor_opt": "人件費最適化・予算組み直し",
    "payroll": "給与見込み・確定（振込なし）",
    "csv_export": "給与CSV出力",
    "qaoa_compare": "注目日 QAOA 比較（利用可能なとき）",
    "advanced_labor": "高度な人件費（深夜/残業/手当）",
    "multi_store": "複数店舗",
}

# Intent / action → required feature
FEATURE_GATES: dict[str, str] = {
    "make_three_plans": "three_plans",
    "replan_lower_cost": "labor_opt",
    "replan_budget": "labor_opt",
    "show_labor_cost": "labor_opt",
    "payroll_month": "payroll",
    "payroll_staff": "payroll",
    "payroll_lock": "payroll",
    "payroll_lock_confirm": "payroll",
    "payroll_csv": "csv_export",
    "payroll_menu": "payroll",
    # month_status: FREE=簡易ダッシュボード / STANDARD+ =フル（manager_dashboard 内で分岐）
    "set_budget": "payroll",
    "set_commute": "advanced_labor",
    "set_allowance": "advanced_labor",
    "set_night_wage": "advanced_labor",
    "set_ot_wage": "advanced_labor",
    "set_actual_hours": "payroll",
    "payslip": "payroll",
}


def _parse_iso(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
        return dt
    except ValueError:
        return None


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def default_subscription_fields(*, trial: bool = True) -> dict[str, Any]:
    """Fields for store create. Default: 14-day STANDARD trial."""
    started = now_local()
    if trial:
        return {
            "plan": PLAN_STANDARD,
            "subscription_status": STATUS_TRIALING,
            "started_at": started.isoformat(timespec="seconds"),
            "current_period_end": (started + timedelta(days=TRIAL_DAYS)).isoformat(
                timespec="seconds"
            ),
            "payment_customer_id": None,
        }
    return {
        "plan": PLAN_FREE,
        "subscription_status": STATUS_NONE,
        "started_at": started.isoformat(timespec="seconds"),
        "current_period_end": None,
        "payment_customer_id": None,
    }


def normalize_plan_code(code: str | None) -> str:
    c = (code or PLAN_FREE).strip().upper()
    return c if c in PLAN_CATALOG else PLAN_FREE


def catalog_entry(plan: str | None) -> dict[str, Any]:
    return dict(PLAN_CATALOG[normalize_plan_code(plan)])


def period_is_valid(store: dict[str, Any] | None) -> bool:
    if not store:
        return False
    end = _parse_iso(store.get("current_period_end"))
    if end is None:
        status = (store.get("subscription_status") or "").lower()
        return status in {STATUS_ACTIVE, STATUS_TRIALING}
    return now_local() <= end


def days_remaining(store: dict[str, Any] | None) -> int | None:
    if not store:
        return None
    end = _parse_iso(store.get("current_period_end"))
    if end is None:
        return None
    delta = end - now_local()
    secs = delta.total_seconds()
    if secs <= 0:
        return 0
    # ceil days
    days = int(secs // 86400)
    if secs % 86400:
        days += 1
    return days


def is_entitled(store: dict[str, Any] | None) -> bool:
    """True when trial/paid period currently unlocks the store plan."""
    if not store:
        return False
    status = (store.get("subscription_status") or STATUS_NONE).lower()
    if status == STATUS_CANCELED:
        return period_is_valid(store)
    if status in {STATUS_EXPIRED, STATUS_PAST_DUE, STATUS_NONE}:
        return False
    if status in {STATUS_ACTIVE, STATUS_TRIALING}:
        return period_is_valid(store)
    return False


def effective_plan(store: dict[str, Any] | None) -> str:
    """Plan unlocking features right now. Expired/failed → FREE (data kept)."""
    if not store:
        return PLAN_FREE
    plan = normalize_plan_code(store.get("plan"))
    if not is_entitled(store):
        return PLAN_FREE
    if plan == PLAN_FREE:
        status = (store.get("subscription_status") or "").lower()
        if status == STATUS_TRIALING and period_is_valid(store):
            return PLAN_STANDARD
        return PLAN_FREE
    return plan


def has_feature(store: dict[str, Any] | None, feature: str) -> bool:
    plan = effective_plan(store)
    return bool(PLAN_CATALOG[plan]["features"].get(feature))


def max_staff_for(store: dict[str, Any] | None) -> int:
    return int(PLAN_CATALOG[effective_plan(store)]["max_staff"])


def max_stores_for(store: dict[str, Any] | None) -> int:
    return int(PLAN_CATALOG[effective_plan(store)]["max_stores_per_manager"])


def required_feature_for_intent(intent: str | None) -> str | None:
    if not intent:
        return None
    return FEATURE_GATES.get(intent)


def upgrade_message(feature: str, store: dict[str, Any] | None = None) -> str:
    need = None
    for code in PLAN_ORDER:
        if PLAN_CATALOG[code]["features"].get(feature):
            need = code
            break
    need_label = PLAN_CATALOG[need]["label"] if need else "有料"
    price = PLAN_CATALOG[need]["price_yen_month"] if need else 980
    cur = effective_plan(store)
    cur_label = PLAN_CATALOG[cur]["label"]
    feat_ja = FEATURE_LABELS_JA.get(feature, feature)
    return (
        f"「{feat_ja}」は【{need_label}】プラン以上の機能です。\n"
        f"現在のプラン: {cur_label}（{cur}）\n"
        f"「プラン」または「料金プラン」で詳細・お申し込み（¥{price:,}/月〜）を確認できます。\n"
        "※ LINE Mini App の IAP は都度課金のみのため、定期プランは外部チェックアウトを使います。\n"
        "解約・期限切れ後も店舗・スタッフ・シフト・給与データは削除しません（機能のみ制限）。"
    )


def subscription_summary(store: dict[str, Any] | None) -> dict[str, Any]:
    """Structured view for UI / API."""
    stored_plan = normalize_plan_code((store or {}).get("plan"))
    eff = effective_plan(store)
    status = ((store or {}).get("subscription_status") or STATUS_NONE).lower()
    days = days_remaining(store)
    cat = catalog_entry(eff)
    stored_cat = catalog_entry(stored_plan)
    return {
        "store_id": (store or {}).get("store_id"),
        "plan": stored_plan,
        "plan_label": stored_cat["label"],
        "effective_plan": eff,
        "effective_label": cat["label"],
        "subscription_status": status,
        "started_at": (store or {}).get("started_at"),
        "current_period_end": (store or {}).get("current_period_end"),
        "payment_customer_id": (store or {}).get("payment_customer_id"),
        "days_remaining": days,
        "entitled": is_entitled(store),
        "price_yen_month": cat["price_yen_month"],
        "max_staff": cat["max_staff"],
        "features": dict(cat["features"]),
        "trial_days": TRIAL_DAYS,
    }


def format_plan_status_text(store: dict[str, Any] | None) -> str:
    s = subscription_summary(store)
    lines = [
        "【料金プラン】",
        f"店舗: {(store or {}).get('store_name') or '—'}（{s['store_id'] or '—'}）",
        f"契約プラン: {s['plan_label']}（{s['plan']}）",
        f"有効プラン: {s['effective_label']}（{s['effective_plan']}）",
        f"ステータス: {s['subscription_status']}",
    ]
    if s["subscription_status"] == STATUS_TRIALING:
        rem = s["days_remaining"]
        if rem is None:
            lines.append(f"トライアル: {TRIAL_DAYS}日間（残り日数不明）")
        else:
            lines.append(f"トライアル残り: {rem} 日（全{TRIAL_DAYS}日）")
    elif s["days_remaining"] is not None and s["entitled"]:
        lines.append(f"期間残り: {s['days_remaining']} 日")
    elif not s["entitled"] and s["plan"] != PLAN_FREE:
        lines.append("※ 期限切れまたは未払いのため、有料機能は停止中です（データは保持）。")

    lines.append("")
    lines.append("■ 有効中の機能")
    for key, on in s["features"].items():
        mark = "✓" if on else "—"
        lines.append(f"  {mark} {FEATURE_LABELS_JA.get(key, key)}")

    lines.append("")
    lines.append("■ プラン一覧")
    for code in PLAN_ORDER:
        c = PLAN_CATALOG[code]
        price = "¥0" if c["price_yen_month"] == 0 else f"¥{c['price_yen_month']:,}/月"
        mark = "◀ 現在" if code == s["effective_plan"] else ""
        lines.append(f"・{c['label']}（{code}）{price} — {c['blurb']} {mark}".rstrip())

    lines.append("")
    lines.append("申し込む: 「申し込む スタンダード」または「申し込む プロ」")
    lines.append("（ボタンの「申し込む」からも可）")
    return "\n".join(lines)


def apply_expiry_if_needed(store: dict[str, Any]) -> dict[str, Any]:
    """Return patch fields if trial/paid period ended. Does not delete data."""
    status = (store.get("subscription_status") or "").lower()
    if status not in {STATUS_TRIALING, STATUS_ACTIVE, STATUS_CANCELED}:
        return {}
    if period_is_valid(store):
        return {}
    # expire entitlement; keep plan field for history but mark expired
    return {
        "subscription_status": STATUS_EXPIRED,
        # keep plan / started_at / payment_customer_id / period_end for audit
    }
