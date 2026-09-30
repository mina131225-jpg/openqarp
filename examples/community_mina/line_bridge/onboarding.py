"""3分オンボーディング（友だち追加 → 店舗作成 → 同意 → 招待 → サンプルシフト）。

ここは LINE メッセージ（Flex / quick reply）の組み立てと進捗判定だけを持つ。
状態の保存は stores.py、ディスパッチは webhook_app.py。
正直表記: シフト本体は古典ソルバ。OpenQARP（量子アプリ）は PoC／比較用。
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from billing import max_staff_for, subscription_summary
from invite_share import (
    friend_add_url,
    invite_image_message,
    invite_page_path,
    invite_share_text,
    line_share_url,
    public_https_base,
    referral_share_text,
)
from line_ui import encode_postback, qr_postback, with_quick_reply
from shift_messages import POC_BRANDING_COPY

SETUP_STEPS: list[tuple[str, str]] = [
    ("store", "お店を登録"),
    ("consent", "注意事項に同意（無料トライアル開始）"),
    ("invite", "スタッフを招待"),
    ("first_plan", "はじめてのシフト作成"),
]

SAMPLE_LABELS = {"A": "サンプル佐藤", "B": "サンプル鈴木", "C": "サンプル高橋", "D": "サンプル田中"}
LINE_URI_MAX = 1000

# ---- text → onboarding intent ---------------------------------------------

_EXACT = {
    "お店を始める": "onboard_manager",
    "お店を始める（店長）": "onboard_manager",
    "お店を始める(店長)": "onboard_manager",
    "店長として始める": "onboard_manager",
    "スタッフとして参加": "onboard_staff",
    "続きから": "resume_setup",
    "セットアップ": "resume_setup",
    "スタッフを招待": "show_invite",
    "スタッフ招待": "show_invite",
    "招待": "show_invite",
    "招待QR": "invite_qr",
    "QRコード": "invite_qr",
    "サンプルでシフトを作ってみる": "sample_plans",
    "サンプルシフト": "sample_plans",
    "このサービスを他の店長に紹介": "refer_service",
    "他の店長に紹介": "refer_service",
    "スキップ": "skip_name",
    "キャンセル": "cancel_input",
}


def parse_onboarding_text(text: str) -> dict[str, Any] | None:
    t = (text or "").strip().replace("　", " ")
    if t in _EXACT:
        return {"intent": _EXACT[t], "raw": t}
    m = re.match(r"^紹介コード\s*[:：]?\s*(?P<code>[A-Za-z0-9]{4,12})$", t)
    if m:
        return {"intent": "apply_referral", "raw": t, "code": m.group("code").upper()}
    return None


ONBOARDING_POSTBACKS = {
    "onboard_manager",
    "onboard_staff",
    "resume_setup",
    "show_invite",
    "invite_qr",
    "sample_plans",
    "refer_service",
    "skip_name",
    "cancel_input",
}

# ---- progress ----------------------------------------------------------------


def setup_status(store: dict[str, Any] | None, *, consented: bool) -> dict[str, Any]:
    ob = (store or {}).get("onboarding") or {}
    members = (store or {}).get("members") or []
    done = {
        "store": store is not None,
        "consent": bool(store) and consented,
        "invite": bool(ob.get("invite_shown")) or len(members) >= 2,
        "first_plan": bool(ob.get("first_plan"))
        or bool((store or {}).get("pending_plans"))
        or bool((store or {}).get("confirmed_plan")),
    }
    count = sum(1 for k, _ in SETUP_STEPS if done[k])
    nxt = next((k for k, _ in SETUP_STEPS if not done[k]), None)
    return {"done": done, "count": count, "total": len(SETUP_STEPS), "next": nxt}


def progress_text(status: dict[str, Any]) -> str:
    n, total = status["count"], status["total"]
    bar = "■" * n + "□" * (total - n)
    lines = [f"セットアップ {n}/{total} {bar}"]
    for key, label in SETUP_STEPS:
        lines.append(("✅ " if status["done"][key] else "⬜ ") + label)
    return "\n".join(lines)


def staff_count_text(store: dict[str, Any]) -> str:
    return f"{len(store.get('members') or [])}/{max_staff_for(store)}名"


# ---- flex primitives -------------------------------------------------------------


def _pb_button(label: str, action: str, *, style: str = "secondary", display: str | None = None) -> dict[str, Any]:
    return {
        "type": "button",
        "style": style,
        "height": "sm",
        "action": {
            "type": "postback",
            "label": label[:40],
            "data": encode_postback(action=action),
            "displayText": (display or label)[:300],
        },
    }


def _uri_button(label: str, uri: str, *, style: str = "primary") -> dict[str, Any]:
    return {"type": "button", "style": style, "height": "sm", "action": {"type": "uri", "label": label[:40], "uri": uri}}


def _bubble(title: str, lines: list[str], buttons: list[dict[str, Any]], *, alt: str, header_color: str = "#06C755") -> dict[str, Any]:
    body = [
        {"type": "text", "text": ln, "size": "sm", "wrap": True, "color": "#334155", "margin": "sm"}
        for ln in lines if ln
    ]
    return {
        "type": "flex",
        "altText": alt[:400],
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": header_color,
                "paddingAll": "14px",
                "contents": [{"type": "text", "text": title, "weight": "bold", "size": "md", "color": "#ffffff", "wrap": True}],
            },
            "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": body or [{"type": "text", "text": " "}]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": buttons},
        },
    }


def resume_items() -> list[dict[str, Any]]:
    return [
        qr_postback("続きから", encode_postback(action="resume_setup"), display_text="続きから"),
        qr_postback("メニュー", encode_postback(action="manager_menu"), display_text="メニュー"),
    ]


# ---- step messages -----------------------------------------------------------


def welcome_messages() -> list[dict[str, Any]]:
    flex = _bubble(
        "友だち追加ありがとうございます",
        [
            "LINE だけでシフト作成・希望休集め・人件費の見込みができます。",
            "店長さんは約3分・タップ中心で最初のシフトまで作れます。",
            POC_BRANDING_COPY,
        ],
        [
            _pb_button("お店を始める（店長）", "onboard_manager", style="primary"),
            _pb_button("スタッフとして参加", "onboard_staff", style="secondary"),
        ],
        alt="ようこそ｜お店を始める（店長）／スタッフとして参加",
    )
    items = [
        qr_postback("お店を始める（店長）", encode_postback(action="onboard_manager"), display_text="お店を始める（店長）"),
        qr_postback("スタッフとして参加", encode_postback(action="onboard_staff"), display_text="スタッフとして参加"),
        qr_postback("使い方", encode_postback(action="help"), display_text="使い方"),
    ]
    return [with_quick_reply(flex, items)]


def ask_store_name_messages() -> list[dict[str, Any]]:
    return [with_quick_reply(
        {
            "type": "text",
            "text": (
                "セットアップ 0/4 □□□□\n"
                "お店の名前を送ってください（入力はここだけです）。\n"
                "例: 青山カフェ"
            ),
        },
        [qr_postback("キャンセル", encode_postback(action="cancel_input"), display_text="キャンセル")],
    )]


def ask_invite_code_messages() -> list[dict[str, Any]]:
    return [with_quick_reply(
        {
            "type": "text",
            "text": (
                "店長から届いた招待コード（6文字の英数字）を送ってください。\n"
                "例: AB12CD\n"
                "※ 招待メッセージの「登録 ○○○○ お名前」をそのまま送ってもOKです。"
            ),
        },
        [
            qr_message_item("デモ店舗で試す", "登録 DEMO01"),
            qr_postback("キャンセル", encode_postback(action="cancel_input"), display_text="キャンセル"),
        ],
    )]


def qr_message_item(label: str, text: str) -> dict[str, Any]:
    return {"type": "action", "action": {"type": "message", "label": label[:20], "text": text}}


def ask_name_messages(store_name: str) -> list[dict[str, Any]]:
    return [with_quick_reply(
        {"type": "text", "text": f"「{store_name}」に参加しました。\n最後に、シフト表に出すお名前を送ってください。例: 太郎"},
        [qr_postback("スキップ", encode_postback(action="skip_name"), display_text="スキップ")],
    )]


def store_created_text(store: dict[str, Any], status: dict[str, Any]) -> str:
    return (
        f"お店「{store['store_name']}」を作成しました。\n"
        f"招待コード: {store['invite_code']}\n\n"
        f"{progress_text(status)}\n\n"
        "次は注意事項の確認です（2タップ）。下のボタン「上記を確認しました」→「同意する」を押してください。"
    )


def trial_started_text(store: dict[str, Any]) -> str:
    s = subscription_summary(store)
    end = (s.get("current_period_end") or "")[:10]
    if s.get("subscription_status") == "trialing":
        days = s.get("days_remaining")
        return (
            f"🎉 {s.get('trial_days', 14)}日間の無料トライアル（{s.get('plan_label')}）を開始しました。"
            f"残り{days}日（〜{end}）。クレジットカード登録は不要です。"
        )
    return f"現在のプラン: {s.get('effective_label')}（{s.get('subscription_status')}）"


def invite_payload(store: dict[str, Any], base: str | None) -> dict[str, Any]:
    code = store["invite_code"]
    https = public_https_base(base)
    page_url = (https + invite_page_path(code)) if https else None
    text = invite_share_text(store["store_name"], code, page_url=page_url)
    share = line_share_url(text)
    if len(share) > LINE_URI_MAX:
        share = line_share_url(invite_share_text(store["store_name"][:12], code))
    if len(share) > LINE_URI_MAX:  # pragma: no cover - safety net
        share = line_share_url(f"友だち追加 {friend_add_url()} → 「登録 {code} お名前」")
    return {"text": text, "share_url": share, "page_url": page_url, "code": code}


def invite_step_messages(store: dict[str, Any], status: dict[str, Any], base: str | None) -> list[dict[str, Any]]:
    """スタッフ招待（転送用テキスト＋シェアボタン＋QR）。reply 上限 5 件以内。"""
    p = invite_payload(store, base)
    buttons = [_uri_button("LINEで招待を送る", p["share_url"], style="primary")]
    buttons.append(_pb_button("QRコードを表示", "invite_qr"))
    if p["page_url"]:
        buttons.append(_uri_button("招待ページを開く", p["page_url"], style="secondary"))
    buttons.append(_pb_button("サンプルでシフトを作ってみる", "sample_plans", style="primary"))
    buttons.append(_pb_button("他の店長に紹介", "refer_service", style="link"))
    flex = _bubble(
        f"スタッフを招待（{progress_text(status).splitlines()[0]}）",
        [
            f"参加中: {staff_count_text(store)}",
            "「LINEで招待を送る」→ 送り先のトーク／グループを選ぶだけで届きます。",
            "上の招待文を長押しして転送、または QR を店内に掲示しても OK。",
            "スタッフが揃う前でも「サンプルでシフトを作ってみる」で出来上がりを確認できます。",
        ],
        buttons,
        alt=f"スタッフを招待｜招待コード {p['code']}",
    )
    return [
        {"type": "text", "text": p["text"]},
        with_quick_reply(flex, resume_items()),
    ]


def invite_qr_messages(store: dict[str, Any], base: str | None) -> list[dict[str, Any]]:
    img = invite_image_message(base, store["invite_code"])
    p = invite_payload(store, base)
    if img is None:
        return [{
            "type": "text",
            "text": (
                "QR 画像の配信には公開 HTTPS の URL（PUBLIC_BASE_URL）が必要です。\n"
                f"代わりに友だち追加 URL を共有してください: {friend_add_url()}\n"
                f"招待コード: {store['invite_code']}"
            ),
        }]
    note = "この QR を店内に掲示／スタッフに見せてください。読み取り → 友だち追加 →「スタッフとして参加」→ 招待コード入力で完了です。"
    if p["page_url"]:
        note += f"\n招待ページ: {p['page_url']}"
    return [img, with_quick_reply({"type": "text", "text": note}, resume_items())]


def refer_messages(referral_code: str | None) -> list[dict[str, Any]]:
    text = referral_share_text(referral_code)
    flex = _bubble(
        "このサービスを他の店長に紹介",
        [
            "紹介文にはお店の情報（店舗名・スタッフ・時給）は含まれません。",
            f"紹介コード: {referral_code}" if referral_code else "",
        ],
        [_uri_button("LINEで紹介を送る", line_share_url(text))],
        alt="このサービスを他の店長に紹介",
        header_color="#1e40af",
    )
    return [{"type": "text", "text": text}, with_quick_reply(flex, resume_items())]


def setup_done_text(status: dict[str, Any]) -> str:
    return f"{progress_text(status)}\n\nセットアップ完了です！「メニュー」からいつでも操作できます。"


def join_notice_text(store: dict[str, Any], display_name: str | None, worker_id: str | None) -> str:
    who = f"{display_name}さん" if display_name else f"スタッフ（枠 {worker_id or '—'}）"
    return f"{who}が参加しました（{staff_count_text(store)}）"


# ---- sample plans ------------------------------------------------------------


def real_staff_count(store: dict[str, Any] | None) -> int:
    return len((store or {}).get("members") or [])


def sample_scenario(base_scenario: dict[str, Any]) -> dict[str, Any]:
    sc = deepcopy(base_scenario)
    sc["_display_labels"] = {w: SAMPLE_LABELS.get(w, f"サンプル{w}") for w in sc.get("workers") or []}
    sc["_sample"] = True
    return sc


def strip_confirm_buttons(flex: dict[str, Any]) -> dict[str, Any]:
    """サンプルは確定・配信させない（確定ボタンを除去してサンプル表記に置換）。"""
    out = deepcopy(flex)
    for b in (out.get("contents") or {}).get("contents") or []:
        footer = b.get("footer") or {}
        kept = [
            c for c in footer.get("contents") or []
            if not (c.get("type") == "button" and "confirm_plan" in str((c.get("action") or {}).get("data")))
            and not (c.get("type") == "text" and "確定" in (c.get("text") or ""))
        ]
        kept.insert(0, {"type": "text", "text": "サンプル（確定・配信されません）", "size": "xs", "weight": "bold", "color": "#b45309", "wrap": True})
        footer["contents"] = kept
        hdr = ((b.get("header") or {}).get("contents") or [])
        if hdr and hdr[0].get("type") == "text":
            hdr[0]["text"] = "【サンプル】" + hdr[0]["text"]
    out["altText"] = "【サンプル】" + (out.get("altText") or "シフト3案")
    return out


def sample_intro_text() -> str:
    return (
        "【サンプル】デモ用スタッフ4名（サンプル佐藤・鈴木・高橋・田中）でシフト3案を作りました。\n"
        "実際のスタッフのデータは使っていません。確定・スタッフ通知もされません。\n"
        "スタッフが2名以上参加すると、同じボタンで本番のシフトを作れます。"
    )


def after_sample_messages(status: dict[str, Any]) -> list[dict[str, Any]]:
    flex = _bubble(
        "次のステップ",
        [progress_text(status), "スタッフを招待すると、本番のシフトを作って LINE で配信できます。"],
        [
            _pb_button("スタッフを招待", "show_invite", style="primary"),
            _pb_button("店長メニュー", "manager_menu"),
        ],
        alt=progress_text(status).splitlines()[0],
    )
    return [with_quick_reply(flex, resume_items())]


def first_plan_messages(status: dict[str, Any], *, can_real: bool) -> list[dict[str, Any]]:
    buttons = [_pb_button("サンプルでシフトを作ってみる", "sample_plans", style="primary")]
    if can_real:
        buttons.insert(0, {
            "type": "button", "style": "primary", "height": "sm",
            "action": {"type": "postback", "label": "本番のシフト3案を作る",
                       "data": encode_postback(action="make_three_plans"), "displayText": "シフト作成"},
        })
    buttons.append(_pb_button("スタッフを招待", "show_invite"))
    flex = _bubble(
        "はじめてのシフト作成",
        [progress_text(status), "ボタン1つで3案（希望優先／人件費優先／バランス）を比較できます。"],
        buttons,
        alt=progress_text(status).splitlines()[0],
    )
    return [with_quick_reply(flex, resume_items())]
