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

from flask import Flask, jsonify, request

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from notify import broadcast_to_store, reply_messages  # noqa: E402
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
    need_register_text,
    POC_BRANDING_COPY,
    parse_user_intent,
    run_shift_for_line,
    scenario_for_store,
)
from stores import (  # noqa: E402
    display_labels_for_store,
    ensure_demo_store,
    get_member,
    get_store_for_user,
    is_user_manager,
    register_manager,
    register_user,
    set_confirmed_plan,
    set_member_display_name,
    set_member_profile,
    set_pending_plans,
    set_store_preferred_offs,
    set_store_wage_premiums,
    staff_profiles_for_store,
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


def handle_text_message(user_id: str, text: str) -> list[dict[str, Any]]:
    """テキスト意図 → LINE messages[]（店舗単位）。"""
    intent = parse_user_intent(text)
    kind = intent.get("intent")

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

    # ---- register / manager ----
    if kind == "register":
        if (intent.get("invite_code") or "").upper() == "DEMO01":
            ensure_demo_store()
        ok, note, store = register_user(
            user_id,
            intent.get("invite_code") or "",
            display_name=intent.get("display_name"),
            worker_alias=intent.get("display_name"),
        )
        if ok and store:
            body = (
                f"{note}\n"
                f"店舗ID: {store['store_id']}\n"
                f"これで「希望休 日曜」「シフト見せて」が使えます。\n"
                f"店長は「シフト3案作って」も利用可。\n"
                f"{POC_BRANDING_COPY}"
            )
        else:
            body = note
        return [{"type": "text", "text": body}]

    if kind == "register_manager":
        if (intent.get("invite_code") or "").upper() == "DEMO01":
            ensure_demo_store()
        ok, note, store = register_manager(
            user_id,
            intent.get("invite_code") or "",
            display_name=intent.get("display_name"),
        )
        body = note if not ok else (
            f"{note}\n"
            f"店長コマンド: 「シフト3案作って」「今週の人件費見せて」"
            f"「人件費を下げて再計算」「確定」\n"
            f"{POC_BRANDING_COPY}"
        )
        return [{"type": "text", "text": body}]

    if kind in {"register_incomplete", "register_manager_incomplete"}:
        return [{
            "type": "text",
            "text": intent.get("hint", "") + "\n\n" + help_text(),
        }]

    if kind == "set_name":
        ok, note, store = set_member_display_name(
            user_id, intent.get("display_name") or ""
        )
        if ok:
            note = f"{note}\n{POC_BRANDING_COPY}"
        return [{"type": "text", "text": note}]

    if kind == "set_name_incomplete":
        return [{
            "type": "text",
            "text": intent.get("hint", "") + "\n\n" + help_text(),
        }]

    if kind == "help" or kind == "set_pref_incomplete":
        msg = help_text()
        if kind == "set_pref_incomplete":
            msg = intent.get("hint", "") + "\n\n" + msg
        store = get_store_for_user(user_id)
        if store:
            member = get_member(store, user_id) or {}
            slot = member.get("worker_id") or "—"
            dn = member.get("display_name") or "（未設定）"
            role = "店長" if member.get("is_manager") else "スタッフ"
            wage = member.get("hourly_wage") or "—"
            msg = (
                f"所属: {store['store_name']}（{store['invite_code']}）\n"
                f"あなたの枠: {slot} ／ 表示名: {dn} ／ 役割: {role} ／ 時給: {wage}\n\n"
                + msg
            )
        else:
            msg = need_register_text() + "\n\n" + msg
        return [{"type": "text", "text": msg}]

    # ---- profile: wage / hour cap / role / premium ----
    if kind == "set_wage":
        store, err = _require_store()
        if err:
            return err
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
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
        if mgr_err and not self_ok:
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

    # ---- set_pref (existing) ----
    if kind == "set_pref":
        store, err = _require_store()
        if err:
            return err
        sc, store = _scenario_for_user(user_id)
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
        set_pending_plans(store["store_id"], bundle_for_storage(bundle))
        # refresh store name
        store = get_store_for_user(user_id) or store
        text_body = header + "\n\n" + format_plans_text(bundle, store_name=store.get("store_name"))
        # LINE reply max 5 messages; text can be long — trim if needed
        if len(text_body) > 4500:
            text_body = text_body[:4400] + "\n…(省略)"
        return [
            {"type": "text", "text": text_body},
            build_plans_flex(bundle, alt_text=header),
        ]

    if kind == "show_labor_cost":
        store, err = _require_store()
        if err:
            return err
        # スタッフも自分の店の予定人件費概要は見られてよい（PoC）。詳細操作は店長。
        if store is None:
            return [{"type": "text", "text": need_register_text()}]
        sc, store = _scenario_for_user(user_id)
        summary = labor_cost_summary(sc, store)
        body = format_labor_cost_text(summary, store_name=store.get("store_name"))
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
        return [{"type": "text", "text": reply}]

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
        body = format_own_shift_text(schedule, sc, wid, display_name=dn) + f"\n{note_src}"
        return [{"type": "text", "text": body}]

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
    return [
        {"type": "text", "text": header},
        build_shift_flex(result),
    ]



def process_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """1 イベントを処理し、reply 結果または None を返す。"""
    etype = event.get("type")
    reply_token = event.get("replyToken") or ""
    source = event.get("source") or {}
    user_id = source.get("userId") or "anonymous"

    if etype == "follow":
        messages = [{"type": "text", "text": canned_follow_reply()}]
        return reply_messages(reply_token, messages)

    if etype == "message":
        msg = event.get("message") or {}
        if msg.get("type") != "text":
            messages = [
                {
                    "type": "text",
                    "text": (
                        "テキストで「登録 店舗コード」「シフト3案作って」"
                        "「シフト見せて」「希望休 日曜」などと送ってください。"
                    ),
                }
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
    """デモ用: 店舗概要（userId はマスク）。"""
    from stores import list_stores, mask_user_id, member_rows_masked

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


@app.post("/demo/message")
def demo_message():
    """資格情報なしのローカル確認用。

    JSON: {"text":"登録 DEMO01","userId":"Udemo"}
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text") or "シフト見せて"
    user_id = data.get("userId") or "Udemo"
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
            "input": {"text": text, "userId": user_id},
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
