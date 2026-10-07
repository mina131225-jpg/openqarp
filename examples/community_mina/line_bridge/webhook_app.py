#!/usr/bin/env python3
"""LINE Messaging API webhook（Flask）— マルチテナント店舗向け。

販売ストーリー:
  店長が Streamlit「店舗向け」で店舗＋招待コードを発行
  → スタッフがお客様の LINE 公式を友だち追加
  → 「登録 <店舗コード>」で userId を店舗に紐付け
  → 「希望休 …」「シフト見せて」は所属店舗のシナリオで動作

機能:
  - X-Line-Signature 検証（Channel secret があるとき）
  - follow / message イベント処理（登録・希望休・組表）
  - トークン未設定時はデモ／モック: ペイロードをログし定型返信を返す
  - 開発者個人 LINE は不要（顧客の公式アカウント想定）

起動:
  cd examples/community_mina/line_bridge
  export LINE_DEMO_MODE=true
  python webhook_app.py

  curl -X POST http://127.0.0.1:8080/webhook \
    -H 'Content-Type: application/json' \
    -d '{"events":[{"type":"message","replyToken":"demo","source":{"userId":"Udemo"},"message":{"type":"text","text":"登録 DEMO01"}}]}'
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, redirect, request

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from notify import broadcast_to_store, push_messages, reply_messages  # noqa: E402
from plans import (  # noqa: E402
    POC_FOOTER,
    bundle_for_storage,
    build_plans_flex,
    format_labor_cost_text,
    format_own_shift_text,
    format_plans_text,
    generate_three_plans,
    labor_cost_summary,
    pick_plan,
    replan_lower_cost,
)
from shift_messages import (  # noqa: E402
    apply_pref_to_scenario,
    build_shift_flex,
    build_shift_text,
    canned_follow_reply,
    connection_status,
    get_line_credentials,
    help_text,
    load_base_scenario,
    need_register_text,
    POC_BRANDING_COPY,
    parse_user_intent,
    run_shift_for_line,
    scenario_for_store,
)
from stores import (  # noqa: E402
    acknowledge_manager_terms,
    clear_pending_payroll_lock,
    consent_manager_terms,
    create_store_as_manager,
    display_labels_for_store,
    ensure_demo_store,
    ensure_referral_code,
    get_member,
    get_pending_payroll_lock,
    get_store_by_invite,
    get_user_state,
    manager_user_ids,
    mark_onboarding,
    set_referred_by,
    set_user_state,
    get_store,
    get_store_for_user,
    is_user_manager,
    manager_terms_status,
    register_manager,
    register_user,
    set_confirmed_plan,
    set_member_display_name,
    set_pending_payroll_lock,
    set_member_profile,
    set_pending_plans,
    set_store_preferred_offs,
    set_store_subscription,
    set_store_wage_premiums,
    staff_profiles_for_store,
)
from billing import (  # noqa: E402
    FEATURE_GATES,
    PLAN_FREE,
    PLAN_PRO,
    PLAN_STANDARD,
    apply_expiry_if_needed,
    effective_plan,
    has_feature,
    normalize_plan_code,
    required_feature_for_intent,
    subscription_summary,
    upgrade_message,
)
from payment_provider import (  # noqa: E402
    MockPaymentProvider,
    check_mock_pay_token,
    consume_mock_pay_token,
    get_payment_provider,
    webhook_secret_is_dev_default,
    period_end_iso,
    public_base_url,
    sign_payload,
    verify_signature,
)
from terms import CAUTION_TEXT, PAYROLL_CONFIRMATION, caution_prompt  # noqa: E402
from line_ui import (  # noqa: E402
    attach_quick_reply_to_last,
    build_manager_menu_flex,
    build_staff_menu_flex,
    consent_ack_items,
    consent_agree_items,
    context_menu_for,
    guest_menu_items,
    legal_links_text,
    manager_menu_items,
    payroll_confirm_items,
    payroll_menu_messages,
    plan_menu_messages,
    postback_to_intent,
    pref_picker_messages,
    staff_mgmt_messages,
    staff_menu_items,
    store_settings_messages,
    with_quick_reply,
)

import onboarding as ob  # noqa: E402
from invite_share import (  # noqa: E402
    invite_landing_html,
    invite_qr_png,
    valid_code,
)
from line_ui import ONBOARDING_ACTIONS, PHASE2_ACTIONS  # noqa: E402
import attendance as att  # noqa: E402
import phase2_ui as p2ui  # noqa: E402
import quantum_compare as qc  # noqa: E402
import shift_rules as sr  # noqa: E402
import command_help as cmdhelp  # noqa: E402
import manager_dashboard as dash  # noqa: E402
import one_tap_shift as ots  # noqa: E402
import slot_optimizer as so  # noqa: E402
from stores import update_store  # noqa: E402

from payroll import (  # noqa: E402
    build_staff_forecast,
    export_payroll_csv,
    find_worker_id,
    format_budget_status_text,
    format_month_payroll_text,
    format_payslip_text,
    format_staff_payroll_text,
    get_locked_payslip,
    lock_month_payroll,
    month_forecast,
    parse_month_token,
    set_actual_hours,
    set_labor_budget,
    set_member_payroll_fields,
)

LOG = logging.getLogger("line_bridge.webhook")

app = Flask(__name__)

# 未登録ユーザー向けの一時シナリオ（プロセス内フォールバック）
_ORPHAN_SCENARIOS: dict[str, dict[str, Any]] = {}


def _verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return False
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _scenario_for_user(user_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """戻り値: (scenario, store_or_none)。店舗があればその preferences を使う。"""
    store = get_store_for_user(user_id)
    if store:
        return scenario_for_store(store), store
    if user_id not in _ORPHAN_SCENARIOS:
        _ORPHAN_SCENARIOS[user_id] = scenario_for_store(None)
    return _ORPHAN_SCENARIOS[user_id], None


def _decorate_menu(user_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach role-appropriate quick reply menu to the last message."""
    if not messages:
        return messages
    store = get_store_for_user(user_id)
    registered = store is not None
    is_mgr = bool(store and is_user_manager(store, user_id))
    return attach_quick_reply_to_last(
        messages, context_menu_for(is_manager=is_mgr, registered=registered)
    )


def _consent_messages(body: str, *, acknowledged: bool) -> list[dict[str, Any]]:
    items = consent_agree_items() if acknowledged else consent_ack_items()
    return [with_quick_reply({"type": "text", "text": body}, items)]


def _menu_or_consent(user_id: str, store: dict[str, Any]) -> list[dict[str, Any]] | None:
    """If manager terms not consented, return consent UI; else None."""
    if not is_user_manager(store, user_id):
        return None
    terms = manager_terms_status(store, user_id)
    if terms.get("consented"):
        return None
    return _consent_messages(
        caution_prompt(acknowledged=bool(terms.get("acknowledged"))),
        acknowledged=bool(terms.get("acknowledged")),
    )




def _feature_gate(store: dict[str, Any] | None, intent: str) -> list[dict[str, Any]] | None:
    """Return upgrade messages if intent requires a paid feature."""
    feat = required_feature_for_intent(intent)
    if not feat:
        return None
    # refresh expiry without deleting data
    if store and apply_expiry_if_needed is not None:
        patch = apply_expiry_if_needed(store)
        if patch and store.get("store_id"):
            set_store_subscription(store["store_id"], **patch)
            store = get_store(store["store_id"]) or store
    if has_feature(store, feat):
        return None
    return _decorate_menu(
        (store or {}).get("_gate_user") or "",
        [{"type": "text", "text": upgrade_message(feat, store)}],
    ) if False else [{"type": "text", "text": upgrade_message(feat, store)}]


def _checkout_urls_for_store(store_id: str, user_id: str) -> dict[str, str]:
    """Best-effort prebuilt checkout URLs (mock/stripe). Failures → empty."""
    out: dict[str, str] = {}
    provider = get_payment_provider()
    for plan in (PLAN_STANDARD, PLAN_PRO):
        try:
            sess = provider.create_checkout_session(
                store_id=store_id, plan=plan, user_id=user_id
            )
            out[plan] = sess.checkout_url
        except Exception as exc:  # noqa: BLE001
            LOG.warning("checkout session create failed plan=%s: %s", plan, exc)
    return out


# ---- 3分オンボーディング／招待・紹介 ------------------------------------------

# テスト・監査用: 直近の参加通知（店長宛て push の本文）。userId 等は含めない。
JOIN_NOTICES: list[dict[str, Any]] = []


def _text(body: str) -> dict[str, Any]:
    return {"type": "text", "text": body}


def _looks_like_command(text: str) -> bool:
    if ob.parse_onboarding_text(text) is not None:
        return True
    kind = parse_user_intent(text).get("intent")
    if kind in {"help", "unknown"}:
        import re as _re
        return bool(_re.search(r"ヘルプ|使い方|help|メニュー", text or "", _re.I))
    return True


def follow_messages(user_id: str) -> list[dict[str, Any]]:
    """友だち追加時: 新規はウェルカム（店長／スタッフ）、既存は「続きから」。"""
    store = get_store_for_user(user_id)
    if store is None:
        return ob.welcome_messages()
    if is_user_manager(store, user_id):
        return [with_quick_reply(
            _text(f"おかえりなさい。「{store['store_name']}」の店長として登録済みです。"),
            ob.resume_items(),
        )]
    return [with_quick_reply(
        _text(f"おかえりなさい。「{store['store_name']}」に登録済みです。"),
        staff_menu_items(),
    )]


def _manager_gate(user_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
    store = get_store_for_user(user_id)
    if store is None:
        return None, ob.welcome_messages()
    if not is_user_manager(store, user_id):
        return None, _decorate_menu(user_id, [_text("この操作は店長のみです。")])
    blocked = _menu_or_consent(user_id, store)
    if blocked:
        return None, blocked
    return store, None


def _consented(store: dict[str, Any] | None, user_id: str) -> bool:
    return bool(store) and bool(manager_terms_status(store, user_id).get("consented"))


def _invite_step(user_id: str, store: dict[str, Any]) -> list[dict[str, Any]]:
    status = ob.setup_status(store, consented=_consented(store, user_id))
    msgs = ob.invite_step_messages(store, status, public_base_url())
    mark_onboarding(store["store_id"], "invite_shown")
    return msgs


def _notify_join(store: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    fresh = get_store(store["store_id"]) or store
    member = get_member(fresh, user_id) or {}
    if member.get("is_manager"):
        return None
    body = ob.join_notice_text(fresh, member.get("display_name"), member.get("worker_id"))
    targets = [u for u in manager_user_ids(fresh) if u != user_id]
    results = []
    for mgr in targets:
        r = push_messages(
            mgr,
            messages=[with_quick_reply(_text(body), [
                qr_postback_item("スタッフ管理", "staff_mgmt"),
                qr_postback_item("シフト作成", "make_three_plans"),
            ])],
        )
        results.append({"mode": r.get("mode"), "sent": r.get("sent")})
    record = {"store_id": fresh["store_id"], "text": body, "targets": len(targets), "results": results}
    JOIN_NOTICES.append(record)
    del JOIN_NOTICES[:-50]
    return record


def qr_postback_item(label: str, action: str) -> dict[str, Any]:
    from line_ui import encode_postback, qr_postback
    return qr_postback(label, encode_postback(action=action), display_text=label)


def _register_staff(user_id: str, code: str, display_name: str | None) -> list[dict[str, Any]]:
    target = get_store_by_invite(code)
    prior = get_store_for_user(user_id)
    already = bool(target and prior and prior["store_id"] == target["store_id"])
    ok, note, store = register_user(
        user_id, code, display_name=display_name, worker_alias=display_name
    )
    if not (ok and store):
        return [_text(note)]
    set_user_state(user_id, None)
    body = (
        f"{note}\n"
        f"店舗ID: {store['store_id']}\n"
        f"下のボタン、または「メニュー」「希望休」「自分のシフト」が使えます。\n"
        f"{POC_BRANDING_COPY}"
    )
    member = get_member(store, user_id) or {}
    if already or member.get("is_manager"):
        return _decorate_menu(user_id, [_text(body)])
    if member.get("display_name"):
        _notify_join(store, user_id)
        return _decorate_menu(user_id, [_text(body)])
    # 名前が未設定 → 名前を聞いてから店長へ参加通知
    set_user_state(user_id, {"kind": "await_name", "store_id": store["store_id"]})
    return ob.ask_name_messages(store["store_name"])


def _handle_pending_input(user_id: str, text: str, state: dict[str, Any]) -> list[dict[str, Any]] | None:
    """入力待ち状態の処理。None なら通常ディスパッチへ。"""
    kind = state.get("kind")
    raw = (text or "").strip().replace("\u3000", " ")
    if kind == "await_store_name":
        if _looks_like_command(raw):
            set_user_state(user_id, None)
            return None
        if len(raw) > 40:
            return [_text("店舗名は40文字以内で送ってください。")] + ob.ask_store_name_messages()
        set_user_state(user_id, None)
        return handle_text_message(user_id, f"店舗作成 {raw}")
    if kind == "await_invite_code":
        code = valid_code(raw.replace(" ", ""))
        if code:
            if code == "DEMO01":
                ensure_demo_store()
            if get_store_by_invite(code) is None:
                return [_text(f"招待コード「{code}」が見つかりません。店長から届いたコードをもう一度確認してください。")] + ob.ask_invite_code_messages()
            return _register_staff(user_id, code, None)
        if _looks_like_command(raw):
            set_user_state(user_id, None)
            return None
        return [_text("招待コードは6文字の英数字です（例: AB12CD）。")] + ob.ask_invite_code_messages()
    if kind == "await_name":
        if _looks_like_command(raw):
            set_user_state(user_id, None)
            return None
        ok, note, store = set_member_display_name(user_id, raw)
        if not ok or store is None:
            return [_text(note)]
        set_user_state(user_id, None)
        _notify_join(store, user_id)
        return _decorate_menu(user_id, [_text(f"{note}\n準備完了です。希望休はボタンから出せます。")])
    set_user_state(user_id, None)
    return None


def _sample_plans(user_id: str) -> list[dict[str, Any]]:
    store, err = _manager_gate(user_id)
    if err:
        return err
    assert store is not None
    if ob.real_staff_count(store) >= 2:
        return handle_text_message(user_id, "シフト3案作って")
    sc = ob.sample_scenario(load_base_scenario())
    bundle = generate_three_plans(sc, None)
    body = format_plans_text(bundle, store_name=f"{store['store_name']}（サンプル）")
    body = body.replace(
        "確定する案を選んで「確定」「確定 希望」「確定 2」などと送ってください。",
        "※ サンプルのため確定・スタッフ通知はされません。",
    )
    text_body = ob.sample_intro_text() + "\n\n" + body
    if len(text_body) > 4500:
        text_body = text_body[:4400] + "\n…(省略)"
    flex = ob.strip_confirm_buttons(build_plans_flex(bundle, alt_text="シフト3案"))
    store = mark_onboarding(store["store_id"], "first_plan") or store
    status = ob.setup_status(store, consented=True)
    return [_text(text_body), flex] + ob.after_sample_messages(status)


def _resume(user_id: str) -> list[dict[str, Any]]:
    store = get_store_for_user(user_id)
    if store is None:
        st = get_user_state(user_id) or {}
        if st.get("kind") == "await_store_name":
            return ob.ask_store_name_messages()
        if st.get("kind") == "await_invite_code":
            return ob.ask_invite_code_messages()
        return ob.welcome_messages()
    if not is_user_manager(store, user_id):
        return handle_text_message(user_id, "スタッフメニュー")
    consented = _consented(store, user_id)
    status = ob.setup_status(store, consented=consented)
    nxt = status["next"]
    if nxt == "consent":
        terms = manager_terms_status(store, user_id)
        ack = bool(terms.get("acknowledged"))
        return _consent_messages(
            ob.progress_text(status) + "\n\n" + caution_prompt(acknowledged=ack),
            acknowledged=ack,
        )
    if nxt == "invite":
        return _invite_step(user_id, store)
    if nxt == "first_plan":
        return ob.first_plan_messages(status, can_real=ob.real_staff_count(store) >= 2)
    flex = build_manager_menu_flex(store_name=store.get("store_name"))
    return attach_quick_reply_to_last(
        [_text(ob.setup_done_text(status)), flex], manager_menu_items()
    )


def _onboarding_action(user_id: str, kind: str, intent: dict[str, Any]) -> list[dict[str, Any]]:
    if kind == "onboard_manager":
        store = get_store_for_user(user_id)
        if store is not None:
            if is_user_manager(store, user_id):
                return _resume(user_id)
            return _decorate_menu(user_id, [_text(
                f"既に「{store['store_name']}」のスタッフとして登録されています。"
                "別のお店を開く場合は「店舗作成 店舗名」と送ってください。"
            )])
        set_user_state(user_id, {"kind": "await_store_name"})
        return ob.ask_store_name_messages()
    if kind == "onboard_staff":
        store = get_store_for_user(user_id)
        if store is not None:
            return handle_text_message(user_id, "スタッフメニュー")
        set_user_state(user_id, {"kind": "await_invite_code"})
        return ob.ask_invite_code_messages()
    if kind == "cancel_input":
        set_user_state(user_id, None)
        if get_store_for_user(user_id) is None:
            return [_text("キャンセルしました。")] + ob.welcome_messages()
        return _decorate_menu(user_id, [_text("キャンセルしました。")])
    if kind == "skip_name":
        st = get_user_state(user_id) or {}
        set_user_state(user_id, None)
        store = get_store_for_user(user_id)
        if store and st.get("kind") == "await_name":
            _notify_join(store, user_id)
        return _decorate_menu(user_id, [_text("あとで「名前 太郎」のように設定できます。")])
    if kind == "resume_setup":
        return _resume(user_id)
    if kind == "show_invite":
        store, err = _manager_gate(user_id)
        return err if err else _invite_step(user_id, store)  # type: ignore[arg-type]
    if kind == "invite_qr":
        store, err = _manager_gate(user_id)
        if err:
            return err
        assert store is not None
        mark_onboarding(store["store_id"], "invite_shown")
        return ob.invite_qr_messages(store, public_base_url())
    if kind == "sample_plans":
        return _sample_plans(user_id)
    if kind == "refer_service":
        store = get_store_for_user(user_id)
        code = None
        if store and is_user_manager(store, user_id):
            code = ensure_referral_code(store["store_id"])
        return ob.refer_messages(code)
    if kind == "apply_referral":
        store = get_store_for_user(user_id)
        if store is None or not is_user_manager(store, user_id):
            return [_text("紹介コードは、お店を作成した店長が送ってください（「お店を始める」から開始できます）。")]
        ok, note = set_referred_by(store["store_id"], intent.get("code") or "")
        return _decorate_menu(user_id, [_text(note)])
    return _decorate_menu(user_id, [_text("不明な操作です。「続きから」または「メニュー」を送ってください。")])


# ---- シフト希望・条件付き自動作成・量子比較・勤怠 --------------------------------

QCOMPARE_SYNC_SECONDS = float(os.environ.get("QCOMPARE_SYNC_SECONDS", "8"))
QCOMPARE_JOBS: dict[str, Any] = {}


def _member_store(user_id: str) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]] | None]:
    store = get_store_for_user(user_id)
    if store is None:
        return None, None, _decorate_menu(user_id, [_text(need_register_text())])
    wid = att.member_wid(store, user_id)
    if not wid:
        return None, None, [_text("あなたの枠がありません。")]
    return store, wid, None


def _rules_gate(user_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
    store, err = _manager_gate(user_id)
    return store, err


def _save_availability(user_id: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    store, wid, err = _member_store(user_id)
    if err:
        return err
    assert store and wid
    saved = update_store(store["store_id"], lambda s: sr.set_availability(s, wid, entries)) or store
    lines = [f"{sr.date_label(e['date'])}: {sr.entry_text(sr.availability_for(saved, wid, e['date']) or e)}" for e in entries]
    body = "シフト希望を保存しました。\n" + "\n".join(lines)
    return [with_quick_reply(_text(body), p2ui.after_avail_items())]


def _rule_plans(user_id: str) -> list[dict[str, Any]]:
    store, err = _rules_gate(user_id)
    if err:
        return err
    assert store is not None
    gate = _feature_gate(store, "make_three_plans")
    if gate:
        return _decorate_menu(user_id, gate)
    if len(sr.worker_ids(store)) < 2:
        return [with_quick_reply(_text("条件付き自動作成にはスタッフが2名以上必要です。先にスタッフを招待するか、サンプルで試してください。"),
                                 [qr_postback_item("スタッフを招待", "show_invite"), qr_postback_item("サンプルで試す", "sample_plans")])]
    bundle = so.generate_rule_plans(store)
    update_store(store["store_id"], lambda s: s.__setitem__("pending_rule_plans", bundle))
    mark_onboarding(store["store_id"], "first_plan")
    body = so.format_rule_plans_text(store, bundle)
    if len(body) > 4800:
        body = body[:4700] + "\n…(省略)"
    flex = so.build_rule_plans_flex(bundle, qcompare_enabled=True)
    return [_text(body), with_quick_reply(flex, p2ui.plans_quick_items())]


def _confirm_rule_plan(user_id: str, key: str) -> list[dict[str, Any]]:
    store, err = _rules_gate(user_id)
    if err:
        return err
    assert store is not None
    bundle = store.get("pending_rule_plans") or {}
    plan = next((p for p in bundle.get("plans") or [] if p["key"] == key), None)
    if not plan:
        return [_text("確定できる条件案がありません。先に「来月のシフトを作る」または「条件でシフト作成」を押してください。")]
    dated = so.dated_shifts_from_plan(bundle, plan)
    plabel = bundle.get("period_label") or ots.period_label(bundle.get("dates") or [])

    def _apply(s: dict[str, Any]) -> None:
        s.setdefault("dated_shifts", {}).update(dated)
        s["confirmed_rule_plan"] = {
            "key": plan["key"], "label": plan["label"], "dates": bundle["dates"],
            "metrics": plan["metrics"], "confirmed_by": user_id,
            "confirmed_at": sr.now_jst().isoformat(timespec="seconds"),
            "period_label": plabel, "one_tap": bool(bundle.get("one_tap")),
        }

    store = update_store(store["store_id"], _apply) or store

    def _push(uid: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return push_messages(uid, messages=messages)

    note = ots.notify_staff_own_shifts(store, dated, period_label_s=plabel, push_fn=_push)
    notify_detail = f"スタッフへの個別通知: {note['sent']}/{note['targets']}名"
    card = ots.completion_card(
        store, bundle, plan,
        notify_detail=notify_detail,
        elapsed_ms=(plan.get("seconds") or 0) * 1000,
    )
    from line_ui import encode_postback, qr_postback
    qr = [
        qr_postback("ダッシュボード", encode_postback(action="month_status"), display_text="ダッシュボード"),
        qr_postback("メニュー", encode_postback(action="manager_menu"), display_text="メニュー"),
    ]
    tip = f"【{plan['label']}】で確定しました。スタッフに各自のシフトを通知しました。"
    return [with_quick_reply(_text(tip), qr), with_quick_reply(card, qr)]


def _one_tap_generate(user_id: str, *, treat_missing: bool, preview_days: int | None = None) -> list[dict[str, Any]]:
    store, err = _rules_gate(user_id)
    if err:
        return err
    assert store is not None
    dates = sr.next_month_dates()
    if preview_days is None and not has_feature(store, "three_plans"):
        return ots.free_limit_messages()
    if preview_days is not None:
        dates = dates[:preview_days]
    # フル月は iters を少し抑えて LINE 応答時間を確保
    iters = 400 if len(dates) > 14 else 600
    bundle = ots.generate_month_plans(
        store, dates=dates, treat_missing_unavailable=treat_missing,
        iters=iters, preview_days=preview_days,
    )
    update_store(store["store_id"], lambda s: s.__setitem__("pending_rule_plans", bundle))
    mark_onboarding(store["store_id"], "first_plan")
    qok = has_feature(store, "qaoa_compare")
    return ots.best_plan_messages(store, bundle, qcompare=qok)


def _one_tap_start(user_id: str, *, preview: bool = False) -> list[dict[str, Any]]:
    store, err = _rules_gate(user_id)
    if err:
        return err
    assert store is not None
    if not preview and not has_feature(store, "three_plans"):
        return ots.free_limit_messages()
    if len(sr.worker_ids(store)) < 2:
        return [with_quick_reply(
            _text("来月のシフト作成にはスタッフが2名以上必要です。先にスタッフを招待してください。"),
            [qr_postback_item("スタッフを招待", "show_invite")],
        )]
    dates = sr.next_month_dates()
    if preview:
        dates = dates[:7]
    gate = ots.prefs_gate(store, dates)
    if not gate["complete"]:
        # プレビューでも未提出確認（短い期間）
        update_store(store["store_id"], lambda s: s.__setitem__("one_tap_pending", {
            "dates": dates, "preview": preview, "asked_at": sr.now_jst().isoformat(timespec="seconds"),
        }))
        return ots.incomplete_prefs_messages(store, gate)
    return _one_tap_generate(user_id, treat_missing=False, preview_days=7 if preview else None)


def _qcompare_store_result(store_id: str, res: dict[str, Any]) -> None:
    update_store(store_id, lambda s: s.__setitem__("last_qcompare", res))


def _qcompare_messages(res: dict[str, Any]) -> list[dict[str, Any]]:
    if not res.get("ok"):
        return [_text(res.get("error") or "比較できませんでした。")]
    return [with_quick_reply(qc.build_panel_flex(res), p2ui.qcompare_items())]


def _qcompare(user_id: str, key: str | None) -> list[dict[str, Any]]:
    store, err = _rules_gate(user_id)
    if err:
        return err
    assert store is not None
    if not has_feature(store, "qaoa_compare"):
        return _decorate_menu(user_id, [_text(upgrade_message("qaoa_compare", store))])
    pending = store.get("pending_rule_plans")
    if not pending:
        return [_text("先に「条件でシフト作成」で案を作ってください。")]
    import threading

    sid = store["store_id"]
    box: dict[str, Any] = {}

    def _job() -> None:
        try:
            res = qc.run_compare(store, key or "balance", pending)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("qcompare failed")
            res = {"ok": False, "error": f"比較の実行に失敗しました: {exc}"}
        if res.get("ok"):
            _qcompare_store_result(sid, res)
        box["res"] = res
        if box.get("async"):
            push_messages(user_id, messages=_qcompare_messages(res))

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    th.join(QCOMPARE_SYNC_SECONDS)
    if "res" in box:
        return _qcompare_messages(box["res"])
    box["async"] = True
    QCOMPARE_JOBS[sid] = th
    return [_text("⚛️ 量子方式のシミュレーションを計算中です。終わり次第このトークにお送りします。")]


def _parse_md(token: str | None) -> str | None:
    if not token:
        return sr.now_jst().date().isoformat()
    import re as _re
    m = _re.match(r"^(\d{1,2})[/月](\d{1,2})日?$", token.strip())
    if not m:
        return None
    d = sr._resolve_date(int(m.group(1)), int(m.group(2)), sr.now_jst().date())
    return d.isoformat() if d else None


def _clock(user_id: str, kind: str) -> list[dict[str, Any]]:
    store, wid, err = _member_store(user_id)
    if err:
        return err
    assert store
    box: dict[str, Any] = {}

    def _apply(s: dict[str, Any]) -> None:
        att.sweep_absences(s)
        box["r"] = att.clock_in(s, user_id) if kind == "in" else att.clock_out(s, user_id)

    update_store(store["store_id"], _apply)
    ok, msg = box.get("r") or (False, "記録できませんでした。")
    return [with_quick_reply(_text(msg), p2ui.attendance_items(manager=is_user_manager(store, user_id)))]


def _att_today(user_id: str, d: str | None = None) -> list[dict[str, Any]]:
    store, err = _manager_gate(user_id)
    if err:
        return err
    assert store is not None
    store = update_store(store["store_id"], lambda s: att.sweep_absences(s)) or store
    day = d or sr.now_jst().date().isoformat()
    body = att.format_day_text(store, day) + "\n\n欠勤の登録: 「欠勤 太郎 10/5」／取消: 「欠勤取消 太郎 10/5」"
    return [with_quick_reply(_text(body), p2ui.attendance_items(manager=True))]


def _att_mine(user_id: str) -> list[dict[str, Any]]:
    store, wid, err = _member_store(user_id)
    if err:
        return err
    assert store and wid
    store = update_store(store["store_id"], lambda s: att.sweep_absences(s)) or store
    now = sr.now_jst()
    return [with_quick_reply(_text(att.format_own_month_text(store, wid, now.year, now.month)),
                             p2ui.attendance_items(manager=is_user_manager(store, user_id)))]


def _phase2_action(user_id: str, kind: str, params: dict[str, str]) -> list[dict[str, Any]]:
    if kind == "avail_menu":
        store, wid, err = _member_store(user_id)
        return err or p2ui.avail_picker_messages(sr.planning_dates(store))
    if kind == "avail_pick":
        store, wid, err = _member_store(user_id)
        if err:
            return err
        d = (params.get("date") or "").strip()
        try:
            date_ok = bool(d) and bool(__import__("datetime").date.fromisoformat(d))
        except ValueError:
            date_ok = False
        if not date_ok:
            return p2ui.avail_picker_messages(sr.planning_dates(store))
        return p2ui.avail_options_messages(store, d)
    if kind == "avail_set":
        store, wid, err = _member_store(user_id)
        if err:
            return err
        d = params.get("date") or ""
        st = params.get("st")
        try:
            __import__("datetime").date.fromisoformat(d)
        except ValueError:
            return [_text("日付が不正です。")]
        if st == "ng":
            e = {"date": d, "status": "ng", "start": None, "end": None}
        else:
            slot = next((s for s in sr.get_rules(store)["slots"] if s["name"] == params.get("slot")), None)
            e = {"date": d, "status": "ok", "start": slot["start"] if slot else None, "end": slot["end"] if slot else None}
        return _save_availability(user_id, [e])
    if kind == "avail_done":
        store, wid, err = _member_store(user_id)
        if err:
            return err
        dates = sr.planning_dates(store)
        update_store(store["store_id"], lambda s: sr.mark_done(s, wid, dates[0]))
        return _decorate_menu(user_id, [_text(f"{sr.date_label(dates[0])}〜{sr.date_label(dates[-1])} のシフト希望を提出しました。ありがとうございます！\n"
                                              "※ 記入していない日は「入れない」扱いになります。")])
    if kind == "avail_mine":
        store, wid, err = _member_store(user_id)
        if err:
            return err
        return [with_quick_reply(_text(sr.own_availability_text(store, wid, sr.planning_dates(store))), p2ui.after_avail_items())]
    if kind == "avail_tally":
        store, err = _manager_gate(user_id)
        if err:
            return err
        t = sr.tally(store, sr.planning_dates(store))
        return p2ui.tally_messages(sr.format_tally_text(store, t), has_missing=bool(t["missing"]))
    if kind == "avail_remind":
        store, err = _manager_gate(user_id)
        if err:
            return err
        dates = sr.planning_dates(store)
        t = sr.tally(store, dates)
        uids = [m["user_id"] for m in store.get("members") or [] if str(m.get("worker_id")) in t["missing"] and m.get("user_id") != user_id]
        msg = [with_quick_reply(_text(f"【{store['store_name']}】{sr.date_label(dates[0])}〜{sr.date_label(dates[-1])} のシフト希望をお願いします。"),
                                [qr_postback_item("シフト希望を出す", "avail_menu")])]
        sent = [push_messages(u, messages=msg) for u in uids]
        return [_text(f"未提出 {len(uids)} 名にリマインドを送りました（{'実送信' if any(r.get('sent') for r in sent) else 'デモ／未送信'}）。")]
    if kind == "rule_show":
        store, err = _manager_gate(user_id)
        return err or p2ui.rules_messages(store)  # type: ignore[arg-type]
    if kind in {"rule_req", "rule_mix", "rule_consec"}:
        store, err = _manager_gate(user_id)
        if err:
            return err
        assert store is not None
        if kind == "rule_req":
            cur = next((s for s in sr.get_rules(store)["slots"] if s["name"] == params.get("slot")), None)
            if not cur:
                return [_text("枠が見つかりません。")]
            n = max(0, min(20, int(cur["required"]) + (1 if params.get("d") == "1" else -1)))
            intent = {"intent": "rule_required", "pairs": [(cur["name"], n)]}
        elif kind == "rule_mix":
            intent = {"intent": "rule_mix", "on": params.get("on") == "1"}
        else:
            intent = {"intent": "rule_max_consecutive", "days": int(params.get("n") or 5)}
        return _apply_rule(user_id, store, intent)
    if kind == "one_tap_month":
        return _one_tap_start(user_id, preview=False)
    if kind == "one_tap_preview":
        return _one_tap_start(user_id, preview=True)
    if kind == "one_tap_wait":
        store, err = _manager_gate(user_id)
        if err:
            return err
        assert store is not None
        pending = store.get("one_tap_pending") or {}
        dates = pending.get("dates") or sr.next_month_dates()
        t = sr.tally(store, dates)
        uids = [m["user_id"] for m in store.get("members") or []
                if str(m.get("worker_id")) in t["missing"] and m.get("user_id") != user_id]
        msg = [with_quick_reply(
            _text(f"【{store['store_name']}】{ots.period_label(dates)} のシフト希望をお願いします（来月シフト作成のため）。"),
            [qr_postback_item("シフト希望を出す", "avail_menu")],
        )]
        sent = [push_messages(u, messages=msg) for u in uids]
        body = (f"未提出 {len(uids)} 名に催促しました"
                f"（{'実送信' if any(r.get('sent') for r in sent) else 'デモ／未送信'}）。\n"
                "希望が揃ったら、もう一度「来月のシフトを作る」を押してください。")
        return _decorate_menu(user_id, [_text(body)])
    if kind == "one_tap_force":
        store = get_store_for_user(user_id)
        pending = (store or {}).get("one_tap_pending") or {}
        preview = bool(pending.get("preview"))
        return _one_tap_generate(user_id, treat_missing=True, preview_days=7 if preview else None)
    if kind == "one_tap_show3":
        store, err = _rules_gate(user_id)
        if err:
            return err
        assert store is not None
        bundle = store.get("pending_rule_plans") or {}
        if not bundle.get("plans"):
            return [_text("表示する案がありません。先に「来月のシフトを作る」を押してください。")]
        flex = so.build_rule_plans_flex(bundle, qcompare_enabled=has_feature(store, "qaoa_compare"))
        tip = f"【3案】{bundle.get('period_label') or ''}\n気に入った案の「この案で確定」を押してください。"
        return [_text(tip), with_quick_reply(flex, p2ui.plans_quick_items())]
    if kind == "rule_plans":
        return _rule_plans(user_id)
    if kind == "confirm_rule_plan":
        return _confirm_rule_plan(user_id, params.get("key") or "")
    if kind == "qcompare":
        return _qcompare(user_id, params.get("key"))
    if kind == "qcompare_detail":
        store, err = _manager_gate(user_id)
        if err:
            return err
        res = (store or {}).get("last_qcompare")
        if not res:
            return [_text("まだ比較結果がありません。案の「⚛️ 量子で比べる」を押してください。")]
        return [_text(qc.detail_text(res))]
    if kind == "clock_in":
        return _clock(user_id, "in")
    if kind == "clock_out":
        return _clock(user_id, "out")
    if kind == "att_today":
        return _att_today(user_id)
    if kind == "att_mine":
        return _att_mine(user_id)
    return [_text("不明な操作です。")]


def _apply_rule(user_id: str, store: dict[str, Any], intent: dict[str, Any]) -> list[dict[str, Any]]:
    box: dict[str, Any] = {}

    def _fn(s: dict[str, Any]) -> None:
        box["r"] = sr.apply_rule_intent(s, intent)

    store = update_store(store["store_id"], _fn) or store
    ok, note = box["r"]
    msgs = p2ui.rules_messages(store)
    msgs[0] = with_quick_reply(_text(note + "\n\n" + msgs[0]["text"]), msgs[0]["quickReply"]["items"])
    return msgs


def _phase2_text(user_id: str, text: str) -> list[dict[str, Any]] | None:
    t = (text or "").strip().replace("\u3000", " ")
    exact = {
        "出勤": ("clock_in", {}), "退勤": ("clock_out", {}),
        "シフト希望": ("avail_menu", {}), "シフト希望を出す": ("avail_menu", {}),
        "自分の希望": ("avail_mine", {}), "提出完了": ("avail_done", {}),
        "未提出者にリマインド": ("avail_remind", {}),
        "今日の勤怠": ("att_today", {}), "自分の勤怠": ("att_mine", {}),
        "量子で比べる": ("qcompare", {"key": "balance"}), "⚛️ 量子で比べる": ("qcompare", {"key": "balance"}),
        "量子比較の詳細": ("qcompare_detail", {}),
    }
    if t in exact:
        k, prm = exact[t]
        return _phase2_action(user_id, k, prm)
    if t == "勤怠":
        store = get_store_for_user(user_id)
        if store and is_user_manager(store, user_id):
            return _att_today(user_id)
        return _att_mine(user_id)
    import re as _re
    m = _re.match(r"^勤怠\s+(\S+)$", t)
    if m:
        d = _parse_md(m.group(1))
        return _att_today(user_id, d) if d else [_text("日付は「勤怠 10/5」の形式で指定してください。")]
    m = _re.match(r"^(欠勤取消|欠勤)\s+(\S+?)(?:\s+(\S+))?$", t)
    if m:
        store, err = _manager_gate(user_id)
        if err:
            return err
        assert store is not None
        wid = sr.resolve_name(store, m.group(2))
        d = _parse_md(m.group(3))
        if not wid or not d:
            return [_text("例: 「欠勤 太郎 10/5」（日付省略で今日）")]
        box: dict[str, Any] = {}
        update_store(store["store_id"], lambda s: box.__setitem__("r", att.mark_absent(s, wid, d, undo=m.group(1) == "欠勤取消")))
        return [with_quick_reply(_text(box["r"][1]), p2ui.attendance_items(manager=True))]
    m = _re.match(r"^確定\s*条件案\s*(\S+)$", t) or _re.match(r"^条件案\s*(\S+?)\s*で確定$", t)
    if m:
        key = {"希望優先": "prefer", "人件費優先": "cost", "バランス": "balance", "1": "prefer", "2": "cost", "3": "balance"}.get(m.group(1), m.group(1))
        return _confirm_rule_plan(user_id, key)
    rule = sr.parse_rule_text(t)
    if rule is not None:
        kind = rule["intent"]
        if kind == "rule_show":
            return _phase2_action(user_id, "rule_show", {})
        if kind == "rule_plans":
            return _rule_plans(user_id)
        if kind == "avail_tally":
            return _phase2_action(user_id, "avail_tally", {})
        store, err = _manager_gate(user_id)
        if err:
            return err
        return _apply_rule(user_id, store, rule)  # type: ignore[arg-type]
    entries = sr.parse_availability_text(t)
    if entries is not None:
        return _save_availability(user_id, entries)
    return None


def handle_postback_message(user_id: str, data: str) -> list[dict[str, Any]]:
    """postback data → messages (reuses text intent dispatch)."""
    intent = postback_to_intent(data)
    if intent is None:
        return _decorate_menu(
            user_id,
            [{"type": "text", "text": "不明なボタンです。「メニュー」または「使い方」を送ってください。"}],
        )
    # Prefer typed text synthesis only when needed; menus go through handle_text_message raw.
    kind = intent.get("intent")
    if kind in ONBOARDING_ACTIONS:
        return _onboarding_action(user_id, kind, intent)
    if kind in PHASE2_ACTIONS:
        return _phase2_action(user_id, kind, intent.get("params") or {})
    # set_pref_incomplete from prompt_pref → day picker
    if kind == "set_pref_incomplete" and intent.get("via") == "postback":
        return pref_picker_messages()
    # For intents that map 1:1 to text commands, synthesize to reuse parser/handlers
    synth = {
        "make_three_plans": "シフト3案作って",
        "replan_lower_cost": "人件費を下げて再計算",
        "show_labor_cost": "今週の人件費見せて",
        "show_shift": "シフト見せて",
        "show_own_shift": "自分のシフト",
        "ack_terms": "上記を確認しました",
        "agree": "同意する",
        "payroll_lock_confirm": "確定する",
        "show_terms": "注意事項",
        "help": "使い方",
        "manager_menu": "メニュー",
        "staff_menu": "スタッフメニュー",
        "store_settings": "店舗設定",
        "staff_mgmt": "スタッフ管理",
        "payroll_menu": "人件費・給与",
        "month_status": "今月の状況",
        "show_plans": "料金プラン",
        "legal_links": "利用規約",
    }
    if kind == "subscribe":
        plan = intent.get("plan") or ""
        return handle_text_message(user_id, f"申し込む {plan}".strip())
    if kind == "subscribe_free":
        return handle_text_message(user_id, "申し込む フリー")
    if kind == "confirm_plan":
        sel = intent.get("selector") or ""
        return handle_text_message(user_id, f"確定 {sel}".strip())
    if kind == "set_pref":
        day = intent.get("day") or ""
        return handle_text_message(user_id, f"希望休 {day}")
    if kind == "payroll_month":
        return handle_text_message(user_id, f"給与 {intent.get('month') or '今月'}")
    if kind == "payroll_lock":
        return handle_text_message(user_id, f"給与確定 {intent.get('month') or '今月'}")
    if kind == "payroll_csv":
        return handle_text_message(user_id, f"給与CSV {intent.get('month') or '今月'}")
    if kind in {"register_incomplete", "set_name_incomplete"}:
        # fall into text handler with synthetic incomplete
        return handle_text_message(user_id, "登録" if kind == "register_incomplete" else "名前")
    if kind in synth:
        return handle_text_message(user_id, synth[kind])
    return handle_text_message(user_id, intent.get("raw") or "使い方")


def handle_text_message(user_id: str, text: str) -> list[dict[str, Any]]:
    """テキスト意図 → LINE messages[]（店舗単位）。"""
    ob_intent = ob.parse_onboarding_text(text)
    if ob_intent is not None:
        return _onboarding_action(user_id, ob_intent["intent"], ob_intent)
    state = get_user_state(user_id)
    if state:
        handled = _handle_pending_input(user_id, text, state)
        if handled is not None:
            return handled
    p2 = _phase2_text(user_id, text)
    if p2 is not None:
        return p2
    intent = parse_user_intent(text)
    kind = intent.get("intent")

    # ---- button menus (text fallback labels) ----
    if kind == "manager_menu":
        store = get_store_for_user(user_id)
        if store is None:
            return attach_quick_reply_to_last(
                [{"type": "text", "text": need_register_text()}], guest_menu_items()
            )
        if not is_user_manager(store, user_id):
            flex = build_staff_menu_flex(store_name=store.get("store_name"))
            return attach_quick_reply_to_last(
                [{"type": "text", "text": "スタッフ向けメニューです。"}, flex],
                staff_menu_items(),
            )
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        flex = build_manager_menu_flex(store_name=store.get("store_name"))
        return attach_quick_reply_to_last(
            [{"type": "text", "text": "店長メニューです。ボタンから選んでください。"}, flex],
            manager_menu_items(),
        )

    if kind == "staff_menu":
        store = get_store_for_user(user_id)
        if store is None:
            return attach_quick_reply_to_last(
                [{"type": "text", "text": need_register_text()}], guest_menu_items()
            )
        flex = build_staff_menu_flex(store_name=store.get("store_name"))
        return attach_quick_reply_to_last(
            [{"type": "text", "text": "スタッフメニューです。"}, flex],
            staff_menu_items(),
        )

    if kind == "store_settings":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "店舗設定は店長のみです。"}]
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        return store_settings_messages(store=store)

    if kind == "staff_mgmt":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "スタッフ管理は店長のみです。"}]
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        return staff_mgmt_messages(store=store)

    if kind == "payroll_menu":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "人件費・給与メニューは店長のみです。"}]
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        gate = _feature_gate(store, "payroll_menu")
        if gate:
            return _decorate_menu(user_id, gate)
        return payroll_menu_messages()

    if kind == "show_plans":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        if is_user_manager(store, user_id):
            blocked = _menu_or_consent(user_id, store)
            if blocked:
                return blocked
        urls = {}
        if is_user_manager(store, user_id):
            urls = _checkout_urls_for_store(store["store_id"], user_id)
        return plan_menu_messages(store=store, checkout_urls=urls)

    if kind == "legal_links":
        return _decorate_menu(
            user_id,
            [{"type": "text", "text": legal_links_text(base_url=public_base_url())}],
        )

    if kind == "subscribe_incomplete":
        return _decorate_menu(
            user_id,
            [{"type": "text", "text": intent.get("hint") or "例: 「申し込む スタンダード」"}],
        )

    if kind == "subscribe_free":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        mgr_err = None
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "プラン変更は店長のみです。"}]
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        # Downgrade entitlement to FREE without deleting data
        set_store_subscription(
            store["store_id"],
            plan=PLAN_FREE,
            subscription_status="none",
            current_period_end=None,
        )
        store = get_store(store["store_id"]) or store
        return _decorate_menu(
            user_id,
            [{
                "type": "text",
                "text": (
                    "フリープランに切り替えました。有料機能は利用できませんが、"
                    "店舗・スタッフ・シフト・給与データは保持されます。\n"
                    "「プラン」で再度お申し込みできます。"
                ),
            }],
        )

    if kind == "subscribe":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "お申し込みは店長のみです。"}]
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        plan = normalize_plan_code(intent.get("plan"))
        if plan == PLAN_FREE:
            return handle_text_message(user_id, "申し込む フリー")
        try:
            provider = get_payment_provider()
            sess = provider.create_checkout_session(
                store_id=store["store_id"], plan=plan, user_id=user_id
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("checkout create failed")
            return _decorate_menu(
                user_id,
                [{"type": "text", "text": f"決済セッションを開始できませんでした: {exc}"}],
            )
        body = (
            f"【{plan}】のお申し込み\n"
            f"外部決済ページを開いてください（LINE IAP の定期課金は使いません）。\n"
            f"{sess.checkout_url}\n\n"
            "支払い完了後、成功 webhook で店舗プランが有効化されます。"
            "完了画面から LINE / ミニアプリに戻れます。"
        )
        # Prefer URI button via flex
        from line_ui import build_plans_menu_flex
        flex = build_plans_menu_flex(
            store=store, checkout_urls={plan: sess.checkout_url}
        )
        return attach_quick_reply_to_last(
            [{"type": "text", "text": body}, flex],
            manager_menu_items(),
        )

    if kind == "month_status":
        store = get_store_for_user(user_id)
        if store is None:
            return _decorate_menu(user_id, [{"type": "text", "text": need_register_text()}])
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "ダッシュボードは店長のみです。スタッフの方は「自分のシフト」「出勤」「給与 今月」をご利用ください。"}]
        blocked = _menu_or_consent(user_id, store)
        if blocked:
            return blocked
        sc, store = _scenario_for_user(user_id)
        # sweep absences lazily so 本日の未出勤／欠勤が最新
        from stores import update_store as _upd
        def _refresh(st):
            att.sweep_absences(st)
            now = __import__("shift_rules", fromlist=["now_jst"]).now_jst()
            att.recompute_month_actuals(st, now.year, now.month)
        _upd(store["store_id"], _refresh)
        store = get_store(store["store_id"]) or store
        return dash.dashboard_messages(store, sc)

    if kind == "one_tap_month":
        return _one_tap_start(user_id, preview=False)

    def _require_store() -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        store = get_store_for_user(user_id)
        if store is None and os.environ.get("LINE_REQUIRE_REGISTER", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            if os.environ.get("LINE_ALLOW_ORPHAN", "").strip().lower() not in {
                "1", "true", "yes", "on",
            }:
                return None, [{"type": "text", "text": need_register_text()}]
        return store, None

    def _require_manager(store: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if not is_user_manager(store, user_id):
            return [{
                "type": "text",
                "text": (
                    "この操作は店長のみです。\n"
                    "店長の方は「店長登録 店舗コード」と送るか、"
                    "最初に登録したアカウントで操作してください。"
                ),
            }]
        terms = manager_terms_status(store, user_id)
        if not terms.get("consented"):
            return _consent_messages(
                caution_prompt(acknowledged=bool(terms.get("acknowledged"))),
                acknowledged=bool(terms.get("acknowledged")),
            )
        return None

    def _resolve_who(store: dict[str, Any], who: str | None) -> dict[str, Any] | None:
        """who が枠名/表示名/空(本人)。戻り値は set_member_profile 用 kwargs。"""
        if not who:
            return {"user_id": user_id}
        labels = display_labels_for_store(store)
        # worker_id 直接
        profiles = staff_profiles_for_store(store)
        if who in profiles:
            return {"store_id": store["store_id"], "worker_id": who}
        # 表示名
        for wid, lab in labels.items():
            if lab == who:
                return {"store_id": store["store_id"], "worker_id": wid}
        # 「太郎さん」のさんなしで再試行は呼び出し側で
        return {"store_id": store["store_id"], "display_name": who}

    # ---- 販売版の注意事項・同意（店長機能の入口） ----
    if kind == "show_terms":
        return _consent_messages(CAUTION_TEXT, acknowledged=False)

    if kind in {"ack_terms", "agree"}:
        store = get_store_for_user(user_id)
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if not is_user_manager(store, user_id):
            return [{"type": "text", "text": "この同意フローは店長のみです。"}]
        if kind == "ack_terms":
            ok, note = acknowledge_manager_terms(user_id)
            # After ack, offer agree button
            return _consent_messages(note, acknowledged=True)
        # 「同意する」は、未同意なら規約同意、保留中の給与確定があればその確定にも使う。
        status = manager_terms_status(store, user_id)
        if not status.get("consented"):
            ok, note = consent_manager_terms(user_id)
            if not ok:
                return _decorate_menu(user_id, [{"type": "text", "text": note}])
            store = get_store_for_user(user_id) or store
            head = {
                "type": "text",
                "text": f"{note}\n{ob.trial_started_text(store)}",
            }
            return [head] + _invite_step(user_id, store)
        pending = get_pending_payroll_lock(store, user_id)
        if pending:
            kind = "payroll_lock_confirm"
        else:
            return [{"type": "text", "text": "既に同意済みです。店長機能をご利用いただけます。"}]

    if kind == "payroll_lock_confirm":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        gate = _feature_gate(store, "payroll_lock_confirm")
        if gate:
            return _decorate_menu(user_id, gate)
        gate = _feature_gate(store, "payroll_lock")
        if gate:
            return _decorate_menu(user_id, gate)
        gate = _feature_gate(store, "payroll_csv")
        if gate:
            return _decorate_menu(user_id, gate)
        gate = _feature_gate(store, "payroll_month")
        if gate:
            return _decorate_menu(user_id, gate)
        assert store is not None
        pending = get_pending_payroll_lock(store, user_id)
        if not pending:
            return [{"type": "text", "text": "先に「給与確定 9月」のように対象月を指定してください。"}]
        year, month = int(pending["year"]), int(pending["month"])
        sc, store = _scenario_for_user(user_id)
        ok, note, _ = lock_month_payroll(store, sc, year=year, month=month, locked_by=user_id)
        clear_pending_payroll_lock(store["store_id"], user_id)
        return [{"type": "text", "text": note}]

    # ---- register / manager ----
    if kind == "register":
        if (intent.get("invite_code") or "").upper() == "DEMO01":
            ensure_demo_store()
        return _register_staff(
            user_id, intent.get("invite_code") or "", intent.get("display_name")
        )

    if kind == "register_manager":
        if (intent.get("invite_code") or "").upper() == "DEMO01":
            ensure_demo_store()
        ok, note, store = register_manager(
            user_id,
            intent.get("invite_code") or "",
            display_name=intent.get("display_name"),
        )
        if not ok:
            return [{"type": "text", "text": note}]
        # Gate: show caution with ack button; menu after consent
        body = (
            f"{note}\n"
            f"{CAUTION_TEXT}\n\n"
            f"下のボタンで確認→同意後、店長メニューが使えます。\n"
            f"{POC_BRANDING_COPY}"
        )
        return _consent_messages(body, acknowledged=False)

    if kind in {"register_incomplete", "register_manager_incomplete", "create_store_incomplete"}:
        return [{
            "type": "text",
            "text": intent.get("hint", "") + "\n\n" + help_text(),
        }]

    if kind == "create_store":
        ok, note, store = create_store_as_manager(
            user_id,
            intent.get("store_name") or "",
            display_name=intent.get("display_name"),
        )
        if ok and store:
            set_user_state(user_id, None)
            mark_onboarding(store["store_id"], "store_created")
            status = ob.setup_status(store, consented=False)
            body = (
                f"{note}\n\n"
                f"{ob.progress_text(status)}\n\n"
                f"{CAUTION_TEXT}\n\n"
                f"下のボタンで確認→同意後、店長メニュー（店舗設定｜スタッフ管理｜シフト作成｜人件費・給与｜今月の状況）が使えます。\n"
                f"{POC_BRANDING_COPY}"
            )
            return _consent_messages(body, acknowledged=False)
        return [{"type": "text", "text": note}]

    if kind == "set_name":
        ok, note, store = set_member_display_name(
            user_id, intent.get("display_name") or ""
        )
        if ok:
            note = f"{note}\n{POC_BRANDING_COPY}"
        return _decorate_menu(user_id, [{"type": "text", "text": note}])

    if kind == "set_name_incomplete":
        hint = intent.get("hint") or "「名前 太郎」のように表示名を送ってください。"
        return _decorate_menu(user_id, [{"type": "text", "text": hint}])

    if kind == "set_pref_incomplete":
        # Button-first: show day picker instead of dumping full help
        hint = intent.get("hint") or "希望休の曜日を選んでください。"
        msgs = pref_picker_messages()
        msgs[0] = with_quick_reply(
            {"type": "text", "text": hint + "\n\n" + (msgs[0].get("text") or "")},
            (msgs[0].get("quickReply") or {}).get("items") or [],
        )
        return msgs

    if kind in {"help", "unknown"}:
        store = get_store_for_user(user_id)
        role = cmdhelp.detect_role(store, user_id)
        store_name = (store or {}).get("store_name")
        if kind == "help":
            msg = cmdhelp.role_help_text(role, store_name=store_name)
            if store:
                member = get_member(store, user_id) or {}
                slot = member.get("worker_id") or "—"
                dn = member.get("display_name") or "（未設定）"
                role_jp = "店長" if member.get("is_manager") else "スタッフ"
                wage = member.get("hourly_wage") or "—"
                # invite_code is the user's own store code (isolation: get_store_for_user)
                msg = (
                    f"所属: {store['store_name']}（{store['invite_code']}）\n"
                    f"あなたの枠: {slot} ／ 表示名: {dn} ／ 役割: {role_jp} ／ 時給: {wage}\n\n"
                    + msg
                )
        else:
            suggestion = cmdhelp.suggest_command(text, role=role)
            msg = cmdhelp.unknown_reply_text(
                text, role=role, store_name=store_name, suggestion=suggestion
            )
        items = cmdhelp.help_menu_items(role)
        # If near-miss, prepend a message-type quick reply for the suggested command
        if kind == "unknown":
            suggestion = cmdhelp.suggest_command(text, role=role)
            if suggestion:
                from line_ui import qr_message
                items = [qr_message(f"→ {suggestion}"[:20], suggestion)] + [
                    i for i in items if (i.get("action") or {}).get("text") != suggestion
                ]
                items = items[:13]
        return [with_quick_reply({"type": "text", "text": msg}, items)]

    # ---- profile: wage / hour cap / role / premium ----
    if kind == "set_wage":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        who = intent.get("who")
        # 他人の時給変更は店長のみ
        if who and not is_user_manager(store, user_id):
            member = get_member(store, user_id) or {}
            labels = display_labels_for_store(store)
            self_names = {member.get("worker_id"), member.get("display_name"), labels.get(member.get("worker_id") or "")}
            if who not in self_names:
                return [{"type": "text", "text": "他人の時給変更は店長のみです。"}]
        kwargs = _resolve_who(store, who)
        ok, note, _ = set_member_profile(**kwargs, hourly_wage=int(intent["hourly_wage"]))
        return [{"type": "text", "text": note + (f"\n{POC_BRANDING_COPY}" if ok else "")}]

    if kind == "set_hour_cap":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        mgr_err = _require_manager(store)
        # 本人が自分の上限を言う場合は許可
        who = intent.get("who")
        member = get_member(store, user_id) or {}
        self_ok = who in {
            member.get("worker_id"),
            member.get("display_name"),
            (display_labels_for_store(store) or {}).get(member.get("worker_id") or ""),
        }
        if mgr_err and (is_user_manager(store, user_id) or not self_ok):
            return mgr_err
        kwargs = _resolve_who(store, who)
        ok, note, _ = set_member_profile(
            **kwargs, max_hours_week=float(intent["max_hours_week"])
        )
        return [{"type": "text", "text": note + (f"\n{POC_BRANDING_COPY}" if ok else "")}]

    if kind == "set_role":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        who = intent.get("who")
        if who and not is_user_manager(store, user_id):
            return [{"type": "text", "text": "他人の役割変更は店長のみです。"}]
        kwargs = _resolve_who(store, who)
        ok, note, _ = set_member_profile(**kwargs, role=intent.get("role"))
        return [{"type": "text", "text": note}]

    if kind == "set_premium":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        assert store is not None
        updated = set_store_wage_premiums(
            store["store_id"], {intent["kind"]: float(intent["rate"])}
        )
        prem = (updated or {}).get("wage_premiums") or {}
        return [{
            "type": "text",
            "text": (
                f"割増を更新しました。\n"
                f"土日×{prem.get('weekend')}／祝日×{prem.get('holiday')}／深夜×{prem.get('night')}\n"
                f"（予定人件費シミュレーション用。給与計算ではありません）\n"
                f"{POC_BRANDING_COPY}"
            ),
        }]

    # ---- payroll / budget / commute / allowances / actuals ----
    def _month_or_err(token):
        ym = parse_month_token(token)
        if ym is None:
            return None, [{
                "type": "text",
                "text": f"月の指定が分かりません: {token}\n例: 「今月」「9月」「2026-09」",
            }]
        return ym, None

    if kind == "set_labor_budget":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        gate = _feature_gate(store, "set_budget")
        if gate:
            return _decorate_menu(user_id, gate)
        assert store is not None
        ok, note, _ = set_labor_budget(store["store_id"], int(intent["budget_yen"]))
        if ok:
            sc, store = _scenario_for_user(user_id)
            now = __import__("datetime").datetime.now().astimezone()
            fc = month_forecast(store, sc, year=now.year, month=now.month)
            note = note + "\n\n" + format_budget_status_text(fc, store_name=store.get("store_name"))
        return [{"type": "text", "text": note}]

    if kind == "set_commute":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        gate = _feature_gate(store, "set_commute")
        if gate:
            return _decorate_menu(user_id, gate)
        who = intent.get("who")
        if who and not is_user_manager(store, user_id):
            return [{"type": "text", "text": "他人の交通費変更は店長のみです。"}]
        kwargs = _resolve_who(store, who)
        ok, note, _ = set_member_payroll_fields(
            store["store_id"],
            worker_id=kwargs.get("worker_id"),
            display_name=kwargs.get("display_name"),
            commute_allowance=int(intent["commute_allowance"]),
        )
        # 本人指定で worker/display 無し → user_id から
        if not ok and kwargs.get("user_id"):
            member = get_member(store, user_id) or {}
            ok, note, _ = set_member_payroll_fields(
                store["store_id"],
                worker_id=member.get("worker_id"),
                commute_allowance=int(intent["commute_allowance"]),
            )
        return [{"type": "text", "text": note}]

    if kind == "set_allowance":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        gate = _feature_gate(store, "set_allowance")
        if gate:
            return _decorate_menu(user_id, gate)
        gate = _feature_gate(store, "set_commute")
        if gate:
            return _decorate_menu(user_id, gate)
        assert store is not None
        kwargs = _resolve_who(store, intent.get("who"))
        ok, note, _ = set_member_payroll_fields(
            store["store_id"],
            worker_id=kwargs.get("worker_id"),
            display_name=kwargs.get("display_name"),
            add_allowance={
                "name": intent.get("allowance_name") or "手当",
                "amount": int(intent.get("amount") or 0),
                "type": intent.get("allowance_type") or "monthly",
            },
        )
        return [{"type": "text", "text": note}]

    if kind in {"set_night_wage", "set_ot_wage"}:
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        gate = _feature_gate(store, "set_night_wage")
        if gate:
            return _decorate_menu(user_id, gate)
        who = intent.get("who")
        if who and not is_user_manager(store, user_id):
            return [{"type": "text", "text": "他人の時給変更は店長のみです。"}]
        kwargs = _resolve_who(store, who)
        field = "night_hourly_wage" if kind == "set_night_wage" else "overtime_hourly_wage"
        kw = {
            "worker_id": kwargs.get("worker_id"),
            "display_name": kwargs.get("display_name"),
            field: int(intent["hourly_wage"]),
        }
        ok, note, _ = set_member_payroll_fields(store["store_id"], **kw)
        if not ok and kwargs.get("user_id"):
            member = get_member(store, user_id) or {}
            kw2 = {"worker_id": member.get("worker_id"), field: int(intent["hourly_wage"])}
            ok, note, _ = set_member_payroll_fields(store["store_id"], **kw2)
        return [{"type": "text", "text": note}]

    if kind == "set_actual_hours":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        gate = _feature_gate(store, "set_actual_hours")
        if gate:
            return _decorate_menu(user_id, gate)
        assert store is not None
        ym, merr = _month_or_err(intent.get("month") or "今月")
        if merr:
            return merr
        year, month = ym
        kwargs = _resolve_who(store, intent.get("who"))
        wid = kwargs.get("worker_id")
        if not wid and kwargs.get("display_name"):
            wid = find_worker_id(store, kwargs["display_name"])
        if not wid:
            return [{"type": "text", "text": "対象スタッフが見つかりません。"}]
        if intent.get("actual_hours") is None and intent.get("night_hours") is None and intent.get("ot_hours") is None:
            return [{
                "type": "text",
                "text": "例: 「実績 太郎 80時間」「実績 太郎 80時間 深夜8 残業4」",
            }]
        ok, note, store2 = set_actual_hours(
            store["store_id"],
            year=year,
            month=month,
            worker_id=wid,
            actual_hours=intent.get("actual_hours"),
            night_hours=intent.get("night_hours"),
            ot_hours=intent.get("ot_hours"),
            work_days=intent.get("work_days"),
        )
        if ok and store2:
            sc, _ = _scenario_for_user(user_id)
            row = build_staff_forecast(store2, sc, wid, year=year, month=month)
            if row:
                note = note + "\n\n" + format_staff_payroll_text(row, store_name=store2.get("store_name"))
        return [{"type": "text", "text": note}]

    if kind == "payroll_month":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        ym, merr = _month_or_err(intent.get("month") or "今月")
        if merr:
            return merr
        year, month = ym
        sc, store = _scenario_for_user(user_id)
        # 店長のみ店舗全体の給与一覧。スタッフは自分のみ（他店・同僚の賃金は見せない）
        if not is_user_manager(store, user_id):
            member = get_member(store, user_id) or {}
            wid = member.get("worker_id")
            if not wid:
                return [{"type": "text", "text": "あなたの枠がありません。"}]
            row = build_staff_forecast(store, sc, wid, year=year, month=month)
            if not row:
                return [{"type": "text", "text": "給与見込みを作れませんでした。"}]
            return [{"type": "text", "text": format_staff_payroll_text(row, store_name=store.get("store_name"))}]
        fc = month_forecast(store, sc, year=year, month=month)
        body = format_month_payroll_text(fc, store_name=store.get("store_name"))
        body = format_budget_status_text(fc, store_name=store.get("store_name")) + "\n\n" + body
        if len(body) > 4500:
            body = body[:4400] + "\n…(省略)"
        return [{"type": "text", "text": body}]

    if kind == "payroll_staff":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        ym, merr = _month_or_err(intent.get("month") or "今月")
        if merr:
            return merr
        year, month = ym
        who = intent.get("who")
        member = get_member(store, user_id) or {}
        wid = find_worker_id(store, who)
        if not wid:
            if who in {member.get("display_name"), member.get("worker_id")}:
                wid = member.get("worker_id")
        if not wid:
            return [{"type": "text", "text": f"{who} が見つかりません。表示名または枠名（A/B/C）で指定してください。"}]
        # 店舗内でもスタッフは本人のみ。店長のみ他スタッフ参照可（他店舗は get_store_for_user で既に除外）
        if not is_user_manager(store, user_id) and wid != member.get("worker_id"):
            return [{"type": "text", "text": "他スタッフの給与は店長のみ閲覧できます。"}]
        sc, store = _scenario_for_user(user_id)
        row = build_staff_forecast(store, sc, wid, year=year, month=month)
        if not row:
            return [{"type": "text", "text": "給与見込みを作れませんでした。"}]
        return [{"type": "text", "text": format_staff_payroll_text(row, store_name=store.get("store_name"))}]

    if kind == "payroll_lock":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        gate = _feature_gate(store, "payroll_staff")
        if gate:
            return _decorate_menu(user_id, gate)
        assert store is not None
        ym, merr = _month_or_err(intent.get("month") or "今月")
        if merr:
            return merr
        year, month = ym
        assert store is not None
        set_pending_payroll_lock(store["store_id"], user_id, year, month)
        body = (
            f"{PAYROLL_CONFIRMATION}\n\n"
            f"対象: {year}年{month}月\n"
            "内容を確認したら下のボタン、または「同意する」「確定する」と送ってください。"
        )
        return [with_quick_reply({"type": "text", "text": body}, payroll_confirm_items())]

    if kind == "payroll_payslip":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        gate = _feature_gate(store, "payslip")
        if gate:
            return _decorate_menu(user_id, gate)
        ym, merr = _month_or_err(intent.get("month") or "今月")
        if merr:
            return merr
        year, month = ym
        who = intent.get("who")
        member = get_member(store, user_id) or {}
        wid = find_worker_id(store, who)
        if not wid:
            return [{"type": "text", "text": f"{who} が見つかりません。"}]
        if not is_user_manager(store, user_id) and wid != member.get("worker_id"):
            return [{"type": "text", "text": "他スタッフの明細は店長のみ閲覧できます。"}]
        locked = get_locked_payslip(store, wid, year=year, month=month)
        if locked:
            return [{"type": "text", "text": format_payslip_text(locked, store_name=store.get("store_name"))}]
        # 未ロックなら見込み明細
        sc, store = _scenario_for_user(user_id)
        row = build_staff_forecast(store, sc, wid, year=year, month=month)
        if not row:
            return [{"type": "text", "text": "明細を作れませんでした。"}]
        # wrap as payslip
        slip = {
            "display_name": row["display_name"],
            "worker_id": wid,
            "month_key": row["month_key"],
            "pay": row["current"],
            "rates": row["rates"],
        }
        body = format_payslip_text(slip, store_name=store.get("store_name"))
        body += "\n（未確定の見込み明細です。「給与確定」でロックできます）"
        return [{"type": "text", "text": body}]

    if kind == "payroll_csv":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        assert store is not None
        ym, merr = _month_or_err(intent.get("month") or "今月")
        if merr:
            return merr
        year, month = ym
        sc, store = _scenario_for_user(user_id)
        ok, note, path = export_payroll_csv(store, sc, year=year, month=month)
        return [{"type": "text", "text": note}]

        # ---- set_pref (existing) ----
    if kind == "set_pref":
        store, err = _require_store()
        if err:
            return err
        sc, store = _scenario_for_user(user_id)
        if store and is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        default_worker = None
        if store:
            member = get_member(store, user_id) or {}
            default_worker = member.get("worker_id")
            w_raw = intent.get("worker")
            if w_raw:
                labels = display_labels_for_store(store)
                inv = {v: k for k, v in labels.items() if v and v != k}
                if w_raw in inv:
                    intent = dict(intent)
                    intent["worker"] = inv[w_raw]
                # 他枠の希望休は店長のみ（店舗内でもクロス枠書込を防ぐ）
                resolved = intent.get("worker") or w_raw
                if (
                    resolved
                    and resolved != default_worker
                    and resolved != member.get("display_name")
                    and not is_user_manager(store, user_id)
                ):
                    return [{
                        "type": "text",
                        "text": "他スタッフの希望休変更は店長のみです。自分の希望は「希望休 日曜」と送ってください。",
                    }]
        sc2, note = apply_pref_to_scenario(
            sc,
            worker=intent.get("worker"),
            day=intent.get("day"),
            default_worker=default_worker,
        )
        if store:
            set_store_preferred_offs(store["store_id"], sc2.get("preferred_offs") or {})
            note = f"[{store['store_name']}] {note}"
        else:
            _ORPHAN_SCENARIOS[user_id] = sc2
        result = run_shift_for_line(sc2)
        return [
            {"type": "text", "text": note},
            build_shift_flex(result, alt_text=note),
        ]

    # ---- manager: 3 plans / labor / confirm ----
    if kind in {"make_three_plans", "replan_lower_cost", "replan_budget"}:
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        assert store is not None
        gate = _feature_gate(store, kind)
        if gate:
            return _decorate_menu(user_id, gate)
        sc, store = _scenario_for_user(user_id)
        budget = intent.get("budget_yen") if kind == "replan_budget" else None
        if kind == "replan_lower_cost" or kind == "replan_budget":
            bundle = replan_lower_cost(sc, store, budget_yen=budget)
            header = "人件費を抑えた再計算結果です。"
            if budget:
                header = f"人件費 {budget:,}円以内を意識した再計算結果です。"
        else:
            bundle = generate_three_plans(sc, store)
            header = "シフト3案を作りました（古典ソルバの重み違い）。"
        if not has_feature(store, "qaoa_compare"):
            bundle = dict(bundle)
            bundle["quantum_compare"] = {
                "available": False,
                "skipped": True,
                "reason": "QAOA比較はプロプラン機能です",
            }
        set_pending_plans(store["store_id"], bundle_for_storage(bundle))
        mark_onboarding(store["store_id"], "first_plan")
        # refresh store name
        store = get_store_for_user(user_id) or store
        text_body = header + "\n\n" + format_plans_text(bundle, store_name=store.get("store_name"))
        # LINE reply max 5 messages; text can be long — trim if needed
        if len(text_body) > 4500:
            text_body = text_body[:4400] + "\n…(省略)"
        msgs = [
            {"type": "text", "text": text_body},
            build_plans_flex(bundle, alt_text=header),
        ]
        return attach_quick_reply_to_last(msgs, manager_menu_items())

    if kind == "show_labor_cost":
        store, err = _require_store()
        if err:
            return err
        # スタッフも自分の店の予定人件費概要は見られてよい（PoC）。詳細操作は店長。
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        if is_user_manager(store, user_id):
            mgr_err = _require_manager(store)
            if mgr_err:
                return mgr_err
        gate = _feature_gate(store, "show_labor_cost")
        if gate:
            return _decorate_menu(user_id, gate)
        sc, store = _scenario_for_user(user_id)
        summary = labor_cost_summary(sc, store)
        body = format_labor_cost_text(summary, store_name=store.get("store_name"))
        # 店長には月次予算ダッシュボードも添付
        if is_user_manager(store, user_id):
            now = __import__("datetime").datetime.now().astimezone()
            fc = month_forecast(store, sc, year=now.year, month=now.month)
            body = format_budget_status_text(fc, store_name=store.get("store_name")) + "\n\n" + body
        return [{"type": "text", "text": body}]

    if kind == "confirm_plan":
        store, err = _require_store()
        if err:
            return err
        mgr_err = _require_manager(store)
        if mgr_err:
            return mgr_err
        assert store is not None
        pending = store.get("pending_plans")
        if not pending or not pending.get("plans"):
            return [{
                "type": "text",
                "text": (
                    "確定できる案がありません。先に「シフト3案作って」を送ってください。"
                ),
            }]
        chosen = pick_plan(pending, intent.get("selector"))
        if not chosen:
            return [{"type": "text", "text": "案の指定が分かりません。例: 「確定 2」「確定 希望」"}]
        # 保存
        confirmed = {
            "key": chosen["key"],
            "label": chosen["label"],
            "schedule": chosen["schedule"],
            "metrics": chosen.get("metrics"),
            "method": chosen.get("method"),
            "confirmed_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).astimezone().isoformat(timespec="seconds"),
            "confirmed_by": user_id,
        }
        set_confirmed_plan(store["store_id"], confirmed)
        store = get_store_for_user(user_id) or store
        m = chosen.get("metrics") or {}
        notice = (
            f"「{store['store_name']}」のシフトを【{chosen['label']}】で確定しました。\n"
            f"希望休 {m.get('pref_ok')}/{m.get('pref_all')} ／ "
            f"予定人件費 {int(m.get('projected_labor_cost') or 0):,}円 ／ "
            f"公平性 {m.get('fairness')}\n"
            f"{POC_FOOTER}"
        )
        # スタッフ通知
        sc, _ = _scenario_for_user(user_id)
        # build a result-like for flex from confirmed schedule
        from plans import compute_metrics
        metrics = compute_metrics(chosen["schedule"], sc, store=store)
        result = {
            "scenario": sc,
            "classical": {
                "schedule": chosen["schedule"],
                "score": metrics.get("solver_score") or 0,
                "seconds": metrics.get("seconds") or 0,
                "pref_hits": [
                    {"worker": h["worker"], "day": h["day"], "granted": h["granted"]}
                    for h in (metrics.get("pref_hits") or [])
                ],
                "focus_on": [],
                "focus_off": [],
            },
        }
        # focus day fill
        focus = sc.get("qaoa_focus_day", "日")
        if focus in sc["days"]:
            fi = sc["days"].index(focus)
            result["classical"]["focus_on"] = sorted(
                w for w in sc["workers"] if chosen["schedule"][w][fi]
            )
            result["classical"]["focus_off"] = sorted(
                w for w in sc["workers"] if not chosen["schedule"][w][fi]
            )
        staff_msgs = [
            {"type": "text", "text": f"【シフト確定のお知らせ】\n{notice}"},
            build_shift_flex(result, alt_text="シフトが確定しました"),
        ]
        bc = broadcast_to_store(store, messages=staff_msgs)
        reply = (
            f"{notice}\n\n"
            f"通知: {bc.get('detail')}\n"
            f"（対象 {len(bc.get('targets') or [])} 名）"
        )
        return _decorate_menu(user_id, [{"type": "text", "text": reply}])

    if kind == "show_own_shift":
        store, err = _require_store()
        if err:
            return err
        sc, store = _scenario_for_user(user_id)
        member = get_member(store, user_id) if store else None
        wid = (member or {}).get("worker_id")
        if not wid:
            return [{"type": "text", "text": "あなたの枠がまだありません。先に「登録」してください。"}]
        # 確定があればそれ、なければライブ古典
        schedule = None
        if store and isinstance(store.get("confirmed_plan"), dict):
            schedule = store["confirmed_plan"].get("schedule")
        if schedule is None:
            result = run_shift_for_line(sc)
            schedule = result["classical"]["schedule"]
            note_src = "（未確定の試算）"
        else:
            note_src = "（確定シフト）"
        dn = (member or {}).get("display_name") or wid
        dated_txt = so.own_dated_shift_text(store, wid) if store else None
        if dated_txt:
            return _decorate_menu(user_id, [_text(dated_txt)])
        body = format_own_shift_text(schedule, sc, wid, display_name=dn) + f"\n{note_src}"
        return _decorate_menu(user_id, [{"type": "text", "text": body}])

    # show_shift（既定）
    store, err = _require_store()
    if err:
        return err

    sc, store = _scenario_for_user(user_id)
    # 確定シフトがあればそれを表示
    if store and isinstance(store.get("confirmed_plan"), dict) and store["confirmed_plan"].get("schedule"):
        conf = store["confirmed_plan"]
        from plans import compute_metrics
        metrics = compute_metrics(conf["schedule"], sc, store=store)
        result = {
            "scenario": sc,
            "classical": {
                "schedule": conf["schedule"],
                "score": metrics.get("solver_score") or 0,
                "seconds": 0,
                "pref_hits": metrics.get("pref_hits") or [],
                "focus_on": [],
                "focus_off": [],
            },
        }
        focus = sc.get("qaoa_focus_day", "日")
        if focus in sc["days"]:
            fi = sc["days"].index(focus)
            result["classical"]["focus_on"] = sorted(
                w for w in sc["workers"] if conf["schedule"][w][fi]
            )
            result["classical"]["focus_off"] = sorted(
                w for w in sc["workers"] if not conf["schedule"][w][fi]
            )
        header = (
            f"「{store['store_name']}」の確定シフト（{conf.get('label')}）です。\n"
            f"{POC_BRANDING_COPY}"
        )
    else:
        result = run_shift_for_line(sc)
        header = f"今週のシフト案です。\n{POC_BRANDING_COPY}"
        if store:
            header = f"「{store['store_name']}」の今週のシフト案です。\n{POC_BRANDING_COPY}"
    return _decorate_menu(
        user_id,
        [
            {"type": "text", "text": header},
            build_shift_flex(result),
        ],
    )


def process_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """1 イベントを処理し、reply 結果または None を返す。"""
    etype = event.get("type")
    reply_token = event.get("replyToken") or ""
    source = event.get("source") or {}
    user_id = source.get("userId") or "anonymous"

    if etype == "follow":
        return reply_messages(reply_token, follow_messages(user_id))

    if etype == "postback":
        data = ((event.get("postback") or {}).get("data")) or ""
        params = (event.get("postback") or {}).get("params") or {}
        if params.get("date") and "date=" not in data:
            data = f"{data}&date={params['date']}"
        LOG.info("postback from %s: %s", user_id, data[:200])
        messages = handle_postback_message(user_id, data)
        return reply_messages(reply_token, messages)

    if etype == "message":
        msg = event.get("message") or {}
        if msg.get("type") != "text":
            messages = [
                with_quick_reply(
                    {
                        "type": "text",
                        "text": (
                            "ボタンまたはテキストで操作できます。\n"
                            "「メニュー」「登録 店舗コード」「シフト見せて」"
                            "「希望休 日曜」など。"
                        ),
                    },
                    guest_menu_items(),
                )
            ]
            return reply_messages(reply_token, messages)
        text = msg.get("text") or ""
        LOG.info("message from %s: %s", user_id, text)
        messages = handle_text_message(user_id, text)
        return reply_messages(reply_token, messages)

    LOG.info("ignore event type=%s", etype)
    return None


@app.get("/")
def index():
    creds = get_line_credentials()
    status = connection_status(creds)
    return jsonify(
        {
            "service": "openqarp-community-mina-line-bridge",
            "status": status,
            "multi_tenant": True,
            "endpoints": {
                "webhook": "POST /webhook",
                "health": "GET /health",
                "demo_message": "POST /demo/message",
                "stores": "GET /stores",
                "billing_checkout": "GET /billing/checkout",
                "billing_webhook": "POST /billing/webhook",
                "billing_success": "GET /billing/success",
                "miniapp": "GET /miniapp",
                "legal_terms": "GET /legal/terms",
                "invite_page": "GET /invite/<code>",
                "invite_qr": "GET /invite/<code>/qr.png",
            },
            "note": (
                "販売時は顧客の LINE 公式を使う想定です。"
                "スタッフは友だち追加後「登録 <店舗コード>」で紐付け。"
                "開発者個人 LINE は不要。"
                "資格情報未設定時はデモモードです。"
            ),
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "status": connection_status(), "multi_tenant": True})


@app.get("/stores")
def stores_list():
    """管理用: 全店舗一覧。トークン必須（未設定時は拒否）。

    マルチテナント隔離のため、招待コード・賃金・メンバー詳細は
    X-Admin-Token == LINE_STORES_ADMIN_TOKEN のときだけ返す。
    """
    from stores import list_stores, mask_user_id, member_rows_masked

    admin = (os.environ.get("LINE_STORES_ADMIN_TOKEN") or "").strip()
    got = (request.headers.get("X-Admin-Token") or request.args.get("token") or "").strip()
    if not admin:
        return jsonify({
            "ok": False,
            "error": "stores listing disabled (set LINE_STORES_ADMIN_TOKEN to enable)",
        }), 403
    if not got or len(got) != len(admin) or not __import__("hmac").compare_digest(got, admin):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    out = []
    for s in list_stores():
        out.append(
            {
                "store_id": s["store_id"],
                "store_name": s["store_name"],
                "invite_code": s["invite_code"],
                "member_count": len(s.get("line_user_ids") or []),
                "members_masked": member_rows_masked(s),
                "line_user_ids_masked": [
                    mask_user_id(u) for u in (s.get("line_user_ids") or [])
                ],
            }
        )
    return jsonify({"ok": True, "stores": out})




@app.get("/billing/checkout")
def billing_checkout():
    """External checkout page (Mock provider completes here)."""
    session_id = (request.args.get("session_id") or "").strip()
    provider = get_payment_provider()
    sess = provider.get_session(session_id) if session_id else None
    if not sess and not _local_or_admin():
        return _pay_denied_page("invalid"), 403
    if not sess:
        return (
            "<!doctype html><html><body><h1>Checkout session not found</h1>"
            "<p><a href='/miniapp'>ミニアプリへ戻る</a></p></body></html>"
        ), 404
    token = (request.args.get("t") or "").strip()
    if not _local_or_admin():
        err = check_mock_pay_token(session_id, token)
        if err is not None:
            LOG.warning("checkout page denied reason=%s", err)
            return _pay_denied_page(err), 403
    plan = sess.get("plan")
    amount = sess.get("amount_yen")
    store_id = sess.get("store_id")
    import html as _html
    token_attr = _html.escape(token, quote=True)
    # Mock: form posts to mock-pay which fires signed webhook then redirects success
    html = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>お申し込み | {plan}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:480px;margin:2rem auto;padding:0 1rem;color:#0f172a}}
.card{{border:1px solid #e2e8f0;border-radius:12px;padding:1.25rem;background:#f8fafc}}
.btn{{display:inline-block;background:#2563eb;color:#fff;padding:.75rem 1.25rem;border-radius:8px;text-decoration:none;border:0;font-size:1rem;cursor:pointer}}
.muted{{color:#64748b;font-size:.85rem}}
</style></head><body>
<h1>料金プランお申し込み</h1>
<div class="card">
<p><strong>{plan}</strong> — ¥{amount:,}/月</p>
<p class="muted">店舗ID: {_html.escape(str(store_id))}</p>
<p class="muted">LINE Mini App IAP は都度課金のみのため、定期プランは外部決済（Payment Provider）を使います。</p>
<form method="post" action="/billing/mock-pay">
  <input type="hidden" name="session_id" value="{_html.escape(session_id, quote=True)}"/>
  <input type="hidden" name="t" value="{token_attr}"/>
  <button class="btn" type="submit">支払いを完了する（デモ）</button>
</form>
<p class="muted" style="margin-top:1rem"><a href="{sess.get('cancel_url') or '/billing/cancel'}">キャンセル</a></p>
</div>
<p class="muted">本番では Stripe 等の Checkout にリダイレクトされます。</p>
</body></html>"""
    return html


@app.post("/billing/mock-pay")
def billing_mock_pay():
    """Demo complete: sign + POST internal webhook, then redirect success."""
    session_id = ""
    if request.form:
        session_id = (request.form.get("session_id") or "").strip()
    elif request.is_json and request.json:
        session_id = str(request.json.get("session_id") or "").strip()
    else:
        session_id = (request.args.get("session_id") or "").strip()
    token = ""
    if request.form:
        token = (request.form.get("t") or "").strip()
    elif request.is_json and request.json:
        token = str(request.json.get("t") or "").strip()
    else:
        token = (request.args.get("t") or "").strip()
    provider = get_payment_provider()
    if not _local_or_admin():
        # トンネル経由は「店長の LINE 申し込みで発行された 15分・1回限りの署名トークン」だけ許可
        if not isinstance(provider, MockPaymentProvider):
            return _pay_denied_page("invalid"), 403
        err = consume_mock_pay_token(session_id, token)
        if err is not None:
            LOG.warning("mock-pay denied reason=%s", err)
            return _pay_denied_page(err), 403
    if not isinstance(provider, MockPaymentProvider):
        # still allow mock completion for local sessions (local/admin only)
        provider = MockPaymentProvider()
    try:
        body = provider.build_success_webhook_body(session_id)
    except KeyError:
        return jsonify({"ok": False, "error": "session not found"}), 404
    sig = sign_payload(body)
    # apply locally (same process)
    from flask import current_app
    with current_app.test_request_context(
        "/billing/webhook",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "X-Payment-Signature": sig},
    ):
        # call handler logic directly
        result, status = _apply_payment_webhook(body, {"X-Payment-Signature": sig})
    sess = provider.get_session(session_id) or {}
    success = sess.get("success_url") or f"/billing/success?session_id={session_id}"
    if status >= 400:
        return jsonify({"ok": False, "result": result}), status
    return redirect(success, code=302)


def _pay_denied_page(reason: str | None) -> str:
    why = {
        "expired": "このお支払いリンクは有効期限（15分）が切れています。",
        "used": "このお支払いリンクは使用済みです。",
    }.get(reason or "", "このお支払いリンクは無効です。")
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>403</title></head>"
        "<body style='font-family:system-ui;max-width:480px;margin:2rem auto;padding:0 1rem'>"
        f"<h1>お手続きできません</h1><p>{why}</p>"
        "<p>店長の LINE から「申し込む プロ」（または「プラン」）を送ると、新しいリンクが発行されます。</p>"
        "<p><a href='https://line.me/R/nv/chat'>LINE に戻る</a></p></body></html>"
    )


def _apply_payment_webhook(body: bytes, headers: dict[str, str]) -> tuple[dict, int]:
    provider = get_payment_provider()
    try:
        # Prefer mock parse for HMAC; stripe provider also accepts our HMAC
        try:
            event = provider.parse_webhook(body, {k: headers.get(k) for k in headers})
        except PermissionError:
            # fallback mock verifier
            event = MockPaymentProvider().parse_webhook(body, headers)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}, 403
    except Exception as exc:  # noqa: BLE001
        LOG.exception("payment webhook parse failed")
        return {"ok": False, "error": str(exc)}, 400

    store_id = (event.get("store_id") or "").strip()
    plan = normalize_plan_code(event.get("plan"))
    if not store_id or plan == PLAN_FREE:
        return {"ok": False, "error": "store_id/plan required"}, 400
    store = get_store(store_id)
    if not store:
        return {"ok": False, "error": "store not found"}, 404

    etype = (event.get("type") or "").lower()
    status = (event.get("status") or "active").lower()

    if etype in {"customer.subscription.deleted", "invoice.payment_failed"} or status in {
        "canceled", "past_due", "expired",
    }:
        # Disable paid features only — never delete operational data
        new_status = "past_due" if "fail" in etype or status == "past_due" else status
        if new_status not in {"past_due", "canceled", "expired"}:
            new_status = "canceled"
        updated = set_store_subscription(
            store_id,
            subscription_status=new_status,
            # keep plan / period / customer for audit; entitlement uses status
        )
        LOG.info("subscription disabled store=%s status=%s (data retained)", store_id, new_status)
        return {
            "ok": True,
            "store_id": store_id,
            "subscription_status": new_status,
            "plan": (updated or {}).get("plan"),
            "data_deleted": False,
        }, 200

    # success → attach plan
    updated = set_store_subscription(
        store_id,
        plan=plan,
        subscription_status="active",
        started_at=(store.get("started_at") or None),
        current_period_end=period_end_iso(1),
        payment_customer_id=event.get("customer_id"),
    )
    if updated and not updated.get("started_at"):
        from billing import now_local
        set_store_subscription(store_id, started_at=now_local().isoformat(timespec="seconds"))
        updated = get_store(store_id)
    LOG.info("subscription activated store=%s plan=%s", store_id, plan)
    return {
        "ok": True,
        "store_id": store_id,
        "plan": plan,
        "subscription_status": "active",
        "current_period_end": (updated or {}).get("current_period_end"),
        "payment_customer_id": (updated or {}).get("payment_customer_id"),
    }, 200


@app.post("/billing/webhook")
def billing_webhook():
    """Payment Provider success / lifecycle webhook → attach or revoke plan on store_id."""
    if webhook_secret_is_dev_default() and not _local_or_admin():
        # 既定の開発用シークレットは公開リポジトリにあるため、外部からの署名は信用しない
        LOG.warning("billing webhook denied: PAYMENT_WEBHOOK_SECRET unset and request not local/admin")
        return jsonify({"ok": False, "error": "forbidden (set PAYMENT_WEBHOOK_SECRET or call locally)"}), 403
    body = request.get_data()
    headers = {k: v for k, v in request.headers.items()}
    result, status = _apply_payment_webhook(body, headers)
    return jsonify(result), status


@app.get("/billing/success")
def billing_success():
    session_id = (request.args.get("session_id") or "").strip()
    provider = get_payment_provider()
    sess = provider.get_session(session_id) if session_id else None
    store = get_store((sess or {}).get("store_id") or "") if sess else None
    plan = (sess or {}).get("plan") or (store or {}).get("plan") or ""
    summary = subscription_summary(store) if store else {}
    html = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>申し込み完了</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:480px;margin:2rem auto;padding:0 1rem}}
.card{{border:1px solid #bbf7d0;background:#f0fdf4;border-radius:12px;padding:1.25rem}}
.btn{{display:inline-block;margin:.4rem .4rem 0 0;background:#16a34a;color:#fff;padding:.7rem 1rem;border-radius:8px;text-decoration:none}}
.btn.secondary{{background:#64748b}}
</style></head><body>
<h1>お申し込み完了</h1>
<div class="card">
<p>プラン <strong>{plan}</strong> を店舗に適用しました。</p>
<p>ステータス: {summary.get('subscription_status') or 'active'}</p>
<p>有効期限: {summary.get('current_period_end') or '—'}</p>
</div>
<p>
  <a class="btn" href="/miniapp">ミニアプリへ戻る（スタブ）</a>
  <a class="btn secondary" href="https://line.me/R/nv/chat">LINE に戻る</a>
</p>
<p style="color:#64748b;font-size:.85rem">LINE トークで「プラン」と送ると現在の契約を確認できます。</p>
</body></html>"""
    return html


@app.get("/billing/cancel")
def billing_cancel():
    return (
        "<!doctype html><html lang='ja'><body style='font-family:system-ui;max-width:480px;margin:2rem auto'>"
        "<h1>申し込みをキャンセルしました</h1>"
        "<p>店舗データは変更されていません。</p>"
        "<p><a href='/miniapp'>ミニアプリへ</a> · <a href='https://line.me/R/nv/chat'>LINE に戻る</a></p>"
        "</body></html>"
    )


@app.get("/miniapp")
def miniapp_stub():
    """LINE Mini App stub — return to chat / show plan status hint."""
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>OpenQARP Mini App Stub</title></head>"
        "<body style='font-family:system-ui;max-width:480px;margin:2rem auto;padding:0 1rem'>"
        "<h1>ミニアプリ（スタブ）</h1>"
        "<p>本番では LINE Mini App の LIFF 画面に置き換えます。</p>"
        "<p>定期課金は Mini App IAP ではなく外部 Payment Provider を使用します。</p>"
        "<p><a href='https://line.me/R/nv/chat'>LINE トークに戻る</a></p>"
        "<p><a href='/legal/terms'>利用規約</a> · "
        "<a href='/legal/privacy'>プライバシー</a> · "
        "<a href='/legal/cancel-refund'>解約・返金</a></p>"
        "</body></html>"
    )


def _read_legal_md(name: str) -> str:
    p = _HERE / name
    if not p.exists():
        return f"# {name}\n\n（ドラフト未配置）\n"
    return p.read_text(encoding="utf-8")


@app.get("/legal/terms")
def legal_terms():
    body = _read_legal_md("利用規約.md")
    return Response(f"<pre style='white-space:pre-wrap;font-family:system-ui;max-width:720px;margin:1rem auto'>{body}</pre>", mimetype="text/html")


@app.get("/legal/privacy")
def legal_privacy():
    body = _read_legal_md("プライバシーポリシー.md")
    return Response(f"<pre style='white-space:pre-wrap;font-family:system-ui;max-width:720px;margin:1rem auto'>{body}</pre>", mimetype="text/html")


@app.get("/legal/cancel-refund")
def legal_cancel_refund():
    body = _read_legal_md("解約・返金ポリシー.md")
    return Response(f"<pre style='white-space:pre-wrap;font-family:system-ui;max-width:720px;margin:1rem auto'>{body}</pre>", mimetype="text/html")



@app.get("/invite/<code>")
def invite_page(code: str):
    """スタッフ向け招待ページ（店舗名＋招待コードのみ表示）。"""
    c = valid_code(code)
    store = get_store_by_invite(c) if c else None
    if not store:
        return Response(
            "<!doctype html><html lang='ja'><meta charset='utf-8'>"
            "<body style='font-family:system-ui;max-width:440px;margin:2rem auto'>"
            "<h1>招待コードが見つかりません</h1><p>店長に招待コードを確認してください。</p></body></html>",
            status=404,
            mimetype="text/html",
        )
    resp = Response(invite_landing_html(store["store_name"], store["invite_code"]), mimetype="text/html")
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/invite/<code>/qr.png")
def invite_qr(code: str):
    c = valid_code(code)
    store = get_store_by_invite(c) if c else None
    if not store:
        return Response(b"not found", status=404, mimetype="text/plain")
    png = invite_qr_png(store["store_name"], store["invite_code"])
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.post("/webhook")
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature")
    creds = get_line_credentials()
    status = connection_status(creds)
    secret = creds.get("channel_secret") or ""

    if secret:
        if not _verify_signature(body, signature, secret):
            LOG.warning("invalid signature")
            return jsonify({"ok": False, "error": "invalid signature"}), 403
    else:
        LOG.info(
            "[DEMO] skip signature check (no LINE_CHANNEL_SECRET). status=%s",
            status,
        )

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "invalid json"}), 400

    LOG.info(
        "webhook received status=%s events=%d body=%s",
        status,
        len(payload.get("events") or []),
        json.dumps(payload, ensure_ascii=False)[:2000],
    )

    results = []
    for event in payload.get("events") or []:
        r = process_event(event)
        if r is not None:
            results.append(
                {
                    "mode": r.get("mode"),
                    "sent": r.get("sent"),
                    "detail": r.get("detail"),
                    "message_count": len((r.get("payload") or {}).get("messages") or []),
                }
            )

    return jsonify(
        {
            "ok": True,
            "status": status,
            "processed": len(results),
            "results": results,
            "demo": status != "接続済",
        }
    ), 200


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# cloudflared / reverse proxies connect from loopback but add these headers.
_PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-Ip", "Forwarded")


def _demo_access_denied():
    """/demo/message は任意 userId になりすませるため、ローカル直アクセス or 管理トークンのみ許可。

    - X-Admin-Token == LINE_DEMO_ADMIN_TOKEN（または LINE_STORES_ADMIN_TOKEN）なら許可
    - それ以外は remote_addr がループバック かつ プロキシ／トンネル経由ヘッダが無いときだけ許可
      （cloudflared は 127.0.0.1 から接続するため、ヘッダで外部経由を判定）
    - LINE_DEMO_ENDPOINT=off で完全無効化
    """
    if (os.environ.get("LINE_DEMO_ENDPOINT") or "").strip().lower() in {"off", "0", "false", "disabled"}:
        return jsonify({"ok": False, "error": "demo endpoint disabled"}), 404
    if _local_or_admin():
        return None
    LOG.warning("demo endpoint denied remote=%s", request.remote_addr)
    return jsonify({"ok": False, "error": "forbidden (local only or X-Admin-Token)"}), 403


def _local_or_admin() -> bool:
    """ローカル直アクセス（ループバック かつ トンネル／プロキシヘッダ無し）または X-Admin-Token 一致。"""
    admin = (os.environ.get("LINE_DEMO_ADMIN_TOKEN") or os.environ.get("LINE_STORES_ADMIN_TOKEN") or "").strip()
    got = (request.headers.get("X-Admin-Token") or "").strip()
    if admin and got and len(got) == len(admin) and hmac.compare_digest(got, admin):
        return True
    remote = (request.remote_addr or "").strip()
    proxied = any(request.headers.get(h) for h in _PROXY_HEADERS)
    return remote in _LOOPBACK and not proxied


@app.post("/demo/message")
def demo_message():
    """資格情報なしのローカル確認用。

    JSON: {"text":"登録 DEMO01","userId":"Udemo"}
         {"postback":"v=1&action=manager_menu","userId":"Udemo"}
    """
    denied = _demo_access_denied()
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    user_id = data.get("userId") or "Udemo"
    postback = data.get("postback")
    text = data.get("text")
    if postback:
        pparams = data.get("params") or {}
        if isinstance(pparams, dict) and pparams.get("date") and "date=" not in postback:
            postback = f"{postback}&date={pparams['date']}"
        messages = handle_postback_message(user_id, postback or "")
    else:
        text = text or "シフト見せて"
        messages = handle_text_message(user_id, text)
    preview_text = None
    store = get_store_for_user(user_id)
    for m in messages:
        if m.get("type") == "flex":
            sc, _ = _scenario_for_user(user_id)
            preview_text = build_shift_text(run_shift_for_line(sc))
            break
        if m.get("type") == "text" and preview_text is None:
            preview_text = m.get("text")
    return jsonify(
        {
            "ok": True,
            "status": connection_status(),
            "input": {"text": text, "postback": postback, "userId": user_id},
            "store": (
                {
                    "store_id": store["store_id"],
                    "store_name": store["store_name"],
                    "invite_code": store["invite_code"],
                }
                if store
                else None
            ),
            "messages": messages,
            "preview_text": preview_text,
        }
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # デモ店舗を用意（販売デモですぐ「登録 DEMO01」できるように）
    try:
        ensure_demo_store()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ensure_demo_store failed: %s", exc)
    host = os.environ.get("LINE_WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("LINE_WEBHOOK_PORT", "8080"))
    status = connection_status()
    LOG.info("starting LINE webhook on %s:%s status=%s", host, port, status)
    if status != "接続済":
        LOG.info(
            "デモ／モックモードです。実 LINE には接続していません。"
            " LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。"
            " 販売時は顧客の公式アカウントを使用（開発者個人 LINE 不要）。"
        )
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
