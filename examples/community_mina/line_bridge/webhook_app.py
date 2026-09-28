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

from notify import reply_messages  # noqa: E402
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
    register_user,
    set_member_display_name,
    set_store_preferred_offs,
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

    if kind == "register":
        # デモ店舗が無い環境でも「登録 DEMO01」が通るよう用意
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
                f"{POC_BRANDING_COPY}"
            )
        else:
            body = note
        return [{"type": "text", "text": body}]

    if kind == "register_incomplete":
        return [
            {
                "type": "text",
                "text": intent.get("hint", "") + "\n\n" + help_text(),
            }
        ]

    if kind == "set_name":
        ok, note, store = set_member_display_name(
            user_id, intent.get("display_name") or ""
        )
        if ok:
            note = f"{note}\n{POC_BRANDING_COPY}"
        return [{"type": "text", "text": note}]

    if kind == "set_name_incomplete":
        return [
            {
                "type": "text",
                "text": intent.get("hint", "") + "\n\n" + help_text(),
            }
        ]

    if kind == "help" or kind == "set_pref_incomplete":
        msg = help_text()
        if kind == "set_pref_incomplete":
            msg = intent.get("hint", "") + "\n\n" + msg
        store = get_store_for_user(user_id)
        if store:
            member = get_member(store, user_id) or {}
            slot = member.get("worker_id") or "—"
            dn = member.get("display_name") or "（未設定）"
            msg = (
                f"所属: {store['store_name']}（{store['invite_code']}）\n"
                f"あなたの枠: {slot} ／ 表示名: {dn}\n\n"
                + msg
            )
        else:
            msg = need_register_text() + "\n\n" + msg
        return [{"type": "text", "text": msg}]

    if kind == "set_pref":
        store = get_store_for_user(user_id)
        if store is None and os.environ.get("LINE_REQUIRE_REGISTER", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            # デモでは未登録でも orphan シナリオで動かすオプション
            if os.environ.get("LINE_ALLOW_ORPHAN", "").strip().lower() not in {
                "1", "true", "yes", "on",
            }:
                return [{"type": "text", "text": need_register_text()}]

        sc, store = _scenario_for_user(user_id)
        # 自分の枠を既定にする（「希望休 日曜」→ 登録順の A/B/C/D）
        default_worker = None
        if store:
            member = get_member(store, user_id) or {}
            default_worker = member.get("worker_id")
            # 表示名で希望休指定された場合も worker_id に解決
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
            # ラベルを維持（apply は deepcopy するが _display_labels もコピーされる）
            set_store_preferred_offs(store["store_id"], sc2.get("preferred_offs") or {})
            note = f"[{store['store_name']}] {note}"
        else:
            _ORPHAN_SCENARIOS[user_id] = sc2
        result = run_shift_for_line(sc2)
        return [
            {"type": "text", "text": note},
            build_shift_flex(result, alt_text=note),
        ]

    # show_shift（既定）
    store = get_store_for_user(user_id)
    if store is None and os.environ.get("LINE_REQUIRE_REGISTER", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        if os.environ.get("LINE_ALLOW_ORPHAN", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            return [{"type": "text", "text": need_register_text()}]

    sc, store = _scenario_for_user(user_id)
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
                        "テキストで「登録 店舗コード」「名前 太郎」"
                        "「シフト見せて」または「希望休 日曜」と送ってください。"
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
