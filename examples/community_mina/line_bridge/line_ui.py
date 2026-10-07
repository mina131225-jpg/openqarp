"""LINE button-first UI helpers (Flex / quick reply / postback).

Text commands remain the fallback; buttons synthesize the same intents.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode

from plans import POC_FOOTER
from shift_messages import POC_BRANDING_COPY

# 3分オンボーディング／招待・紹介（onboarding.py で処理）
ONBOARDING_ACTIONS = frozenset({
    "onboard_manager",
    "onboard_staff",
    "resume_setup",
    "show_invite",
    "invite_qr",
    "sample_plans",
    "refer_service",
    "skip_name",
    "cancel_input",
})

# シフト希望・条件付き自動作成・量子比較・勤怠（webhook_app._phase2_action で処理）
PHASE2_ACTIONS = frozenset({
    "avail_menu", "avail_pick", "avail_set", "avail_done", "avail_mine", "avail_tally", "avail_remind",
    "rule_show", "rule_req", "rule_mix", "rule_consec", "rule_plans", "confirm_rule_plan",
    "qcompare", "qcompare_detail",
    "clock_in", "clock_out", "att_today", "att_mine",
    "one_tap_month", "one_tap_wait", "one_tap_force", "one_tap_show3", "one_tap_preview",
})

# ---- postback codec (data <= 300 bytes) ------------------------------------

def encode_postback(**params: str) -> str:
    """Build compact postback data. Always includes v=1."""
    clean = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
    clean.setdefault("v", "1")
    data = urlencode(clean)
    if len(data) > 300:
        raise ValueError(f"postback data too long ({len(data)}): {data[:80]}…")
    return data


def decode_postback(data: str) -> dict[str, str]:
    raw = (data or "").strip()
    if not raw:
        return {}
    parsed = parse_qs(raw, keep_blank_values=False)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def postback_to_intent(data: str) -> dict[str, Any] | None:
    """Map postback data → intent dict (same shape as parse_user_intent)."""
    p = decode_postback(data)
    action = (p.get("action") or p.get("a") or "").strip()
    if not action:
        return None

    if action == "manager_menu":
        return {"intent": "manager_menu", "raw": data, "via": "postback"}
    if action == "staff_menu":
        return {"intent": "staff_menu", "raw": data, "via": "postback"}
    if action == "store_settings":
        return {"intent": "store_settings", "raw": data, "via": "postback"}
    if action == "staff_mgmt":
        return {"intent": "staff_mgmt", "raw": data, "via": "postback"}
    if action == "payroll_menu":
        return {"intent": "payroll_menu", "raw": data, "via": "postback"}
    if action == "month_status":
        return {"intent": "month_status", "raw": data, "via": "postback"}
    if action == "make_three_plans":
        return {"intent": "make_three_plans", "raw": data, "via": "postback"}
    if action == "replan_lower_cost":
        return {"intent": "replan_lower_cost", "raw": data, "via": "postback"}
    if action == "show_labor_cost":
        return {"intent": "show_labor_cost", "raw": data, "via": "postback"}
    if action == "show_shift":
        return {"intent": "show_shift", "raw": data, "via": "postback"}
    if action == "show_own_shift":
        return {"intent": "show_own_shift", "raw": data, "via": "postback"}
    if action == "confirm_plan":
        sel = (p.get("key") or p.get("sel") or p.get("n") or "").strip() or None
        return {"intent": "confirm_plan", "raw": data, "selector": sel, "via": "postback"}
    if action == "set_pref":
        day = (p.get("day") or "").strip()
        if not day:
            return {"intent": "set_pref_incomplete", "raw": data, "hint": "曜日ボタンを押してください。"}
        return {"intent": "set_pref", "raw": data, "worker": None, "day": day, "day_raw": day, "via": "postback"}
    if action == "ack_terms":
        return {"intent": "ack_terms", "raw": data, "via": "postback"}
    if action == "agree":
        return {"intent": "agree", "raw": data, "via": "postback"}
    if action == "payroll_lock_confirm":
        return {"intent": "payroll_lock_confirm", "raw": data, "month": "今月", "via": "postback"}
    if action == "payroll_month":
        return {"intent": "payroll_month", "raw": data, "month": p.get("month") or "今月", "via": "postback"}
    if action == "payroll_lock":
        return {"intent": "payroll_lock", "raw": data, "month": p.get("month") or "今月", "via": "postback"}
    if action == "payroll_csv":
        return {"intent": "payroll_csv", "raw": data, "month": p.get("month") or "今月", "via": "postback"}
    if action == "help":
        return {"intent": "help", "raw": data, "via": "postback"}
    if action == "prompt_register":
        return {"intent": "register_incomplete", "raw": data, "hint": "「登録 店舗コード」または「登録 店舗コード 名前」と送ってください。"}
    if action == "prompt_name":
        return {"intent": "set_name_incomplete", "raw": data, "hint": "「名前 太郎」のように表示名を送ってください。"}
    if action == "prompt_pref":
        return {"intent": "set_pref_incomplete", "raw": data, "hint": "下の曜日ボタン、または「希望休 日曜」と送ってください。"}
    if action == "show_terms":
        return {"intent": "show_terms", "raw": data, "via": "postback"}
    if action in {"show_plans", "pricing", "plan_menu"}:
        return {"intent": "show_plans", "raw": data, "via": "postback"}
    if action == "subscribe":
        plan = (p.get("plan") or p.get("p") or "").strip().upper() or None
        if not plan:
            return {"intent": "subscribe_incomplete", "raw": data, "hint": "プランを選んでください。", "via": "postback"}
        if plan == "FREE":
            return {"intent": "subscribe_free", "raw": data, "plan": "FREE", "via": "postback"}
        return {"intent": "subscribe", "raw": data, "plan": plan, "via": "postback"}
    if action == "legal_links":
        return {"intent": "legal_links", "raw": data, "via": "postback"}
    if action in ONBOARDING_ACTIONS:
        return {"intent": action, "raw": data, "via": "postback"}
    if action in PHASE2_ACTIONS:
        return {"intent": action, "raw": data, "via": "postback", "params": p}
    return None


# ---- quick reply / button primitives ---------------------------------------

def qr_message(label: str, text: str) -> dict[str, Any]:
    return {
        "type": "action",
        "action": {"type": "message", "label": label[:20], "text": text},
    }


def qr_postback(label: str, data: str, *, display_text: str | None = None) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "postback",
        "label": label[:20],
        "data": data,
    }
    if display_text:
        action["displayText"] = display_text[:300]
    return {"type": "action", "action": action}


def with_quick_reply(message: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach quickReply (max 13) to a copy of the message."""
    msg = dict(message)
    trimmed = items[:13]
    if trimmed:
        msg["quickReply"] = {"items": trimmed}
    return msg


def attach_quick_reply_to_last(
    messages: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not messages or not items:
        return messages
    out = list(messages)
    out[-1] = with_quick_reply(out[-1], items)
    return out


def flex_button_postback(
    label: str,
    data: str,
    *,
    style: str = "primary",
    display_text: str | None = None,
    height: str = "sm",
) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "postback", "label": label[:40], "data": data}
    if display_text:
        action["displayText"] = display_text[:300]
    return {
        "type": "button",
        "style": style,
        "height": height,
        "action": action,
    }


def flex_button_message(
    label: str,
    text: str,
    *,
    style: str = "secondary",
    height: str = "sm",
) -> dict[str, Any]:
    return {
        "type": "button",
        "style": style,
        "height": height,
        "action": {"type": "message", "label": label[:40], "text": text},
    }


def _footer_note() -> dict[str, Any]:
    return {
        "type": "text",
        "text": POC_FOOTER,
        "size": "xxs",
        "color": "#94a3b8",
        "wrap": True,
        "margin": "md",
    }


# ---- role menus ------------------------------------------------------------

def manager_menu_items() -> list[dict[str, Any]]:
    return [
        qr_postback("店舗設定", encode_postback(action="store_settings"), display_text="店舗設定"),
        qr_postback("スタッフ管理", encode_postback(action="staff_mgmt"), display_text="スタッフ管理"),
        qr_postback("来月のシフトを作る", encode_postback(action="one_tap_month"), display_text="来月のシフトを作る"),
        qr_postback("シフト作成", encode_postback(action="make_three_plans"), display_text="シフト作成"),
        qr_postback("人件費・給与", encode_postback(action="payroll_menu"), display_text="人件費・給与"),
        qr_postback("ダッシュボード", encode_postback(action="month_status"), display_text="ダッシュボード"),
        qr_postback("料金プラン", encode_postback(action="show_plans"), display_text="料金プラン"),
        qr_postback("スタッフ招待", encode_postback(action="show_invite"), display_text="スタッフを招待"),
        qr_postback("条件設定", encode_postback(action="rule_show"), display_text="条件設定"),
        qr_postback("提出状況", encode_postback(action="avail_tally"), display_text="提出状況"),
        qr_postback("条件で自動作成", encode_postback(action="rule_plans"), display_text="条件でシフト作成"),
        qr_postback("勤怠", encode_postback(action="att_today"), display_text="今日の勤怠"),
    ]


def staff_menu_items() -> list[dict[str, Any]]:
    return [
        qr_postback("出勤", encode_postback(action="clock_in"), display_text="出勤"),
        qr_postback("退勤", encode_postback(action="clock_out"), display_text="退勤"),
        qr_postback("シフト希望を出す", encode_postback(action="avail_menu"), display_text="シフト希望を出す"),
        qr_postback("自分のシフト", encode_postback(action="show_own_shift"), display_text="自分のシフト"),
        qr_postback("希望休", encode_postback(action="prompt_pref"), display_text="希望休"),
        qr_postback("シフト表", encode_postback(action="show_shift"), display_text="シフト見せて"),
        qr_postback("名前設定", encode_postback(action="prompt_name"), display_text="名前設定"),
        qr_postback("メニュー", encode_postback(action="staff_menu"), display_text="メニュー"),
    ]


def guest_menu_items() -> list[dict[str, Any]]:
    return [
        qr_postback("お店を始める（店長）", encode_postback(action="onboard_manager"), display_text="お店を始める（店長）"),
        qr_postback("スタッフとして参加", encode_postback(action="onboard_staff"), display_text="スタッフとして参加"),
        qr_postback("登録する", encode_postback(action="prompt_register"), display_text="登録"),
        qr_message("登録 DEMO01", "登録 DEMO01"),
        qr_postback("使い方", encode_postback(action="help"), display_text="使い方"),
    ]


def day_pref_items() -> list[dict[str, Any]]:
    days = ["月", "火", "水", "木", "金", "土", "日"]
    return [
        qr_postback(f"{d}曜休", encode_postback(action="set_pref", day=d), display_text=f"希望休 {d}")
        for d in days
    ]


def consent_ack_items() -> list[dict[str, Any]]:
    return [
        qr_postback("上記を確認しました", encode_postback(action="ack_terms"), display_text="上記を確認しました"),
        qr_message("注意事項", "注意事項"),
    ]


def consent_agree_items() -> list[dict[str, Any]]:
    return [
        qr_postback("同意する", encode_postback(action="agree"), display_text="同意する"),
        qr_postback("注意事項", encode_postback(action="show_terms"), display_text="注意事項"),
    ]


def payroll_confirm_items() -> list[dict[str, Any]]:
    return [
        qr_postback("確定する", encode_postback(action="payroll_lock_confirm"), display_text="確定する"),
        qr_postback("同意する", encode_postback(action="agree"), display_text="同意する"),
        qr_postback("キャンセル", encode_postback(action="payroll_menu"), display_text="人件費・給与"),
    ]


def context_menu_for(*, is_manager: bool, registered: bool) -> list[dict[str, Any]]:
    if not registered:
        return guest_menu_items()
    if is_manager:
        return manager_menu_items() + [qr_postback("使い方", encode_postback(action="help"), display_text="使い方")]
    return staff_menu_items()


# ---- Flex menus ------------------------------------------------------------

def build_manager_menu_flex(*, store_name: str | None = None) -> dict[str, Any]:
    title = "店長メニュー"
    if store_name:
        title = f"店長メニュー｜{store_name}"
    buttons = [
        flex_button_postback("店舗設定", encode_postback(action="store_settings"), style="secondary", display_text="店舗設定"),
        flex_button_postback("スタッフ管理", encode_postback(action="staff_mgmt"), style="secondary", display_text="スタッフ管理"),
        flex_button_postback("来月のシフトを作る", encode_postback(action="one_tap_month"), style="primary", display_text="来月のシフトを作る"),
        flex_button_postback("シフト作成", encode_postback(action="make_three_plans"), style="secondary", display_text="シフト作成"),
        flex_button_postback("人件費・給与", encode_postback(action="payroll_menu"), style="secondary", display_text="人件費・給与"),
        flex_button_postback("ダッシュボード", encode_postback(action="month_status"), style="secondary", display_text="ダッシュボード"),
        flex_button_postback("料金プラン", encode_postback(action="show_plans"), style="primary", display_text="料金プラン"),
        flex_button_postback("条件設定", encode_postback(action="rule_show"), style="secondary", display_text="条件設定"),
        flex_button_postback("シフト希望の提出状況", encode_postback(action="avail_tally"), style="secondary", display_text="提出状況"),
        flex_button_postback("条件でシフト自動作成", encode_postback(action="rule_plans"), style="primary", display_text="条件でシフト作成"),
        flex_button_postback("今日の勤怠", encode_postback(action="att_today"), style="secondary", display_text="今日の勤怠"),
        flex_button_postback("スタッフを招待", encode_postback(action="show_invite"), style="secondary", display_text="スタッフを招待"),
        flex_button_postback("他の店長に紹介", encode_postback(action="refer_service"), style="secondary", display_text="他の店長に紹介"),
    ]
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
                    {"type": "text", "text": title, "weight": "bold", "size": "lg", "color": "#0f172a"},
                    {
                        "type": "text",
                        "text": "ボタンで操作できます（テキストコマンドも可）",
                        "size": "xs",
                        "color": "#64748b",
                        "margin": "sm",
                        "wrap": True,
                    },
                ],
                "paddingAll": "14px",
                "backgroundColor": "#f8fafc",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": buttons,
                "spacing": "sm",
                "paddingAll": "14px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [_footer_note()],
                "paddingAll": "12px",
            },
        },
    }


def build_staff_menu_flex(*, store_name: str | None = None) -> dict[str, Any]:
    title = "スタッフメニュー"
    if store_name:
        title = f"スタッフメニュー｜{store_name}"
    buttons = [
        flex_button_postback("出勤", encode_postback(action="clock_in"), style="primary", display_text="出勤"),
        flex_button_postback("退勤", encode_postback(action="clock_out"), style="primary", display_text="退勤"),
        flex_button_postback("シフト希望を出す", encode_postback(action="avail_menu"), style="secondary", display_text="シフト希望を出す"),
        flex_button_postback("自分のシフト", encode_postback(action="show_own_shift"), style="primary", display_text="自分のシフト"),
        flex_button_postback("希望休を選ぶ", encode_postback(action="prompt_pref"), style="secondary", display_text="希望休"),
        flex_button_postback("シフト表を見る", encode_postback(action="show_shift"), style="secondary", display_text="シフト見せて"),
        flex_button_postback("名前を設定", encode_postback(action="prompt_name"), style="secondary", display_text="名前設定"),
    ]
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
                    {"type": "text", "text": title, "weight": "bold", "size": "lg"},
                    {
                        "type": "text",
                        "text": "ボタン優先。詳細はテキストでもOK",
                        "size": "xs",
                        "color": "#64748b",
                        "margin": "sm",
                        "wrap": True,
                    },
                ],
                "paddingAll": "14px",
                "backgroundColor": "#f8fafc",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": buttons,
                "spacing": "sm",
                "paddingAll": "14px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [_footer_note()],
                "paddingAll": "12px",
            },
        },
    }


def build_submenu_flex(
    title: str,
    rows: list[tuple[str, str, str]],
    *,
    intro: str | None = None,
) -> dict[str, Any]:
    """rows: (label, postback_data, display_text)."""
    buttons = [
        flex_button_postback(label, data, style="secondary", display_text=disp)
        for label, data, disp in rows
    ]
    body_contents: list[dict[str, Any]] = []
    if intro:
        body_contents.append(
            {"type": "text", "text": intro, "size": "sm", "color": "#475569", "wrap": True, "margin": "none"}
        )
    body_contents.extend(buttons)
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
                    {"type": "text", "text": title, "weight": "bold", "size": "md"},
                ],
                "paddingAll": "12px",
                "backgroundColor": "#f8fafc",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": body_contents,
                "spacing": "sm",
                "paddingAll": "12px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [_footer_note()],
                "paddingAll": "10px",
            },
        },
    }


def store_settings_messages(*, store: dict[str, Any] | None) -> list[dict[str, Any]]:
    name = (store or {}).get("store_name") or "—"
    code = (store or {}).get("invite_code") or "—"
    sid = (store or {}).get("store_id") or "—"
    intro = (
        f"店舗: {name}\n"
        f"招待コード: {code}\n"
        f"店舗ID: {sid}\n\n"
        f"スタッフには「登録 {code}」を案内してください。\n"
        "詳細設定（割増・予算）はテキストでも可。"
    )
    flex = build_submenu_flex(
        "店舗設定",
        [
            ("注意事項・同意", encode_postback(action="show_terms"), "注意事項"),
            ("料金プラン", encode_postback(action="show_plans"), "料金プラン"),
            ("規約・プライバシー等", encode_postback(action="legal_links"), "利用規約"),
            ("シフト作成へ", encode_postback(action="make_three_plans"), "シフト作成"),
            ("店長メニュー", encode_postback(action="manager_menu"), "メニュー"),
        ],
        intro=intro,
    )
    tip = {
        "type": "text",
        "text": (
            "テキスト例:\n"
            "・人件費予算 200000\n"
            "・割増 土日 1.25\n"
            f"・{POC_BRANDING_COPY}"
        ),
    }
    return attach_quick_reply_to_last([flex, tip], manager_menu_items())


def staff_mgmt_messages(*, store: dict[str, Any] | None) -> list[dict[str, Any]]:
    members = (store or {}).get("members") or []
    lines = ["登録メンバー:"]
    for m in members[:12]:
        role = "店長" if m.get("is_manager") else "スタッフ"
        dn = m.get("display_name") or "（無名）"
        wid = m.get("worker_id") or "?"
        wage = m.get("hourly_wage")
        wage_s = f"／時給{wage}" if wage else ""
        lines.append(f"・{dn}（{wid}・{role}{wage_s}）")
    if not members:
        lines.append("（まだいません）")
    if len(members) > 12:
        lines.append(f"…他 {len(members) - 12} 名")
    flex = build_submenu_flex(
        "スタッフ管理",
        [
            ("シフト作成", encode_postback(action="make_three_plans"), "シフト作成"),
            ("人件費・給与", encode_postback(action="payroll_menu"), "人件費・給与"),
            ("店長メニュー", encode_postback(action="manager_menu"), "メニュー"),
        ],
        intro="\n".join(lines) + "\n\n時給・週上限・手当はテキストで設定できます。",
    )
    tip = {
        "type": "text",
        "text": (
            "テキスト例:\n"
            "・時給 太郎 1200\n"
            "・太郎さんは週20時間以内\n"
            "・交通費 太郎 500\n"
            "・手当 太郎 役職手当 5000"
        ),
    }
    return attach_quick_reply_to_last([flex, tip], manager_menu_items())


def payroll_menu_messages() -> list[dict[str, Any]]:
    flex = build_submenu_flex(
        "人件費・給与",
        [
            ("給与 今月", encode_postback(action="payroll_month", month="今月"), "給与 今月"),
            ("今週の人件費", encode_postback(action="show_labor_cost"), "今週の人件費見せて"),
            ("給与確定（確認）", encode_postback(action="payroll_lock", month="今月"), "給与確定 今月"),
            ("給与CSV 今月", encode_postback(action="payroll_csv", month="今月"), "給与CSV 今月"),
            ("店長メニュー", encode_postback(action="manager_menu"), "メニュー"),
        ],
        intro=(
            "見込み・明細・CSVまで。銀行振込は行いません。\n"
            "給与確定は確認ボタン後にロックされます。"
        ),
    )
    return attach_quick_reply_to_last([flex], manager_menu_items())


def pref_picker_messages() -> list[dict[str, Any]]:
    msg = {
        "type": "text",
        "text": (
            "希望休の曜日を選んでください。\n"
            "テキストでも可: 「希望休 日曜」\n"
            f"{POC_BRANDING_COPY}"
        ),
    }
    return [with_quick_reply(msg, day_pref_items() + staff_menu_items()[:3])]


def confirm_plan_button(plan_key: str, plan_label: str) -> dict[str, Any]:
    return flex_button_postback(
        "この案で確定",
        encode_postback(action="confirm_plan", key=plan_key),
        style="primary",
        display_text=f"確定 {plan_label}",
        height="md",
    )


def flex_button_uri(label: str, uri: str, *, style: str = "primary", height: str = "sm") -> dict[str, Any]:
    return {
        "type": "button",
        "style": style,
        "height": height,
        "action": {"type": "uri", "label": label[:40], "uri": uri},
        "margin": "sm",
    }


def build_plans_menu_flex(*, store: dict | None = None, checkout_urls: dict[str, str] | None = None) -> dict[str, Any]:
    """Current plan + feature list + subscribe buttons (uri or postback)."""
    from billing import PLAN_CATALOG, PLAN_ORDER, subscription_summary

    summary = subscription_summary(store)
    title = "料金プラン"
    status_line = f"{summary['effective_label']} / {summary['subscription_status']}"
    if summary.get("subscription_status") == "trialing" and summary.get("days_remaining") is not None:
        status_line += f"（トライアル残り {summary['days_remaining']} 日）"

    body: list[dict[str, Any]] = [
        {"type": "text", "text": status_line, "size": "sm", "color": "#0f172a", "wrap": True, "weight": "bold"},
        {
            "type": "text",
            "text": "LINE IAP は都度課金のみのため、定期プランは外部決済です。",
            "size": "xxs",
            "color": "#64748b",
            "wrap": True,
            "margin": "md",
        },
    ]
    for code in PLAN_ORDER:
        c = PLAN_CATALOG[code]
        price = "¥0" if c["price_yen_month"] == 0 else f"¥{c['price_yen_month']:,}/月"
        mark = "【利用中】" if code == summary["effective_plan"] else ""
        body.append(
            {
                "type": "text",
                "text": f"{c['label']} {price} {mark}\n{c['blurb']}".strip(),
                "size": "xs",
                "color": "#334155",
                "wrap": True,
                "margin": "md",
            }
        )
        if code in {"STANDARD", "PRO"} and code != summary["effective_plan"]:
            url = (checkout_urls or {}).get(code)
            if url:
                body.append(flex_button_uri(f"{c['label']}に申し込む", url, style="primary"))
            else:
                body.append(
                    flex_button_postback(
                        f"{c['label']}に申し込む",
                        encode_postback(action="subscribe", plan=code),
                        style="primary",
                        display_text=f"申し込む {c['label']}",
                    )
                )
    body.append(
        flex_button_postback(
            "規約・解約・返金",
            encode_postback(action="legal_links"),
            style="secondary",
            display_text="利用規約",
        )
    )
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
                    {"type": "text", "text": title, "weight": "bold", "size": "lg", "color": "#0f172a"},
                ],
                "paddingAll": "14px",
                "backgroundColor": "#f8fafc",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": body,
                "spacing": "sm",
                "paddingAll": "14px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [_footer_note()],
                "paddingAll": "12px",
            },
        },
    }


def plan_menu_messages(*, store: dict | None, checkout_urls: dict[str, str] | None = None) -> list[dict[str, Any]]:
    from billing import format_plan_status_text

    text_msg = {"type": "text", "text": format_plan_status_text(store)}
    flex = build_plans_menu_flex(store=store, checkout_urls=checkout_urls)
    return attach_quick_reply_to_last([text_msg, flex], manager_menu_items())


def legal_links_text(*, base_url: str = "") -> str:
    root = (base_url or "").rstrip("/")
    terms = f"{root}/legal/terms" if root else "利用規約.md"
    privacy = f"{root}/legal/privacy" if root else "プライバシーポリシー.md"
    cancel = f"{root}/legal/cancel-refund" if root else "解約・返金ポリシー.md"
    return (
        "【規約・プライバシー・解約/返金】\n"
        f"・利用規約: {terms}\n"
        f"・プライバシーポリシー: {privacy}\n"
        f"・解約・返金: {cancel}\n"
        "\n"
        "解約や期限切れ後も、店舗・スタッフ・シフト・給与データは削除しません。"
        "有料機能のみ利用できなくなります。\n"
        "※ LINE Mini App IAP による定期課金は行いません（都度課金のみのため外部決済）。"
    )
