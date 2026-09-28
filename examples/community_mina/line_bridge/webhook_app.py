#!/usr/bin/env python3
"""LINE Messaging API webhook（Flask）。

機能:
  - X-Line-Signature 検証（Channel secret があるとき）
  - follow / message イベント処理
  - 「希望休 日曜」「シフト見せて」等に古典ソルバ結果で返信
  - トークン未設定時はデモ／モック: ペイロードをログし定型返信を返す

起動:
  cd examples/community_mina/line_bridge
  export LINE_DEMO_MODE=true   # 資格情報なしで試す
  python webhook_app.py

  curl -X POST http://127.0.0.1:8080/webhook \\
    -H 'Content-Type: application/json' \\
    -d '{"events":[{"type":"message","replyToken":"demo","source":{"userId":"Udemo"},"message":{"type":"text","text":"シフト見せて"}}]}'
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
    load_base_scenario,
    parse_user_intent,
    run_shift_for_line,
)

LOG = logging.getLogger("line_bridge.webhook")

app = Flask(__name__)

# ユーザーごとの簡易希望休（プロセス内。PoC 用）
_USER_SCENARIOS: dict[str, dict[str, Any]] = {}


def _verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return False
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _scenario_for_user(user_id: str) -> dict[str, Any]:
    if user_id not in _USER_SCENARIOS:
        _USER_SCENARIOS[user_id] = load_base_scenario()
    return _USER_SCENARIOS[user_id]


def handle_text_message(user_id: str, text: str) -> list[dict[str, Any]]:
    """テキスト意図 → LINE messages[]。"""
    intent = parse_user_intent(text)
    kind = intent.get("intent")

    if kind == "help" or kind == "set_pref_incomplete":
        msg = help_text()
        if kind == "set_pref_incomplete":
            msg = intent.get("hint", "") + "\n\n" + msg
        return [{"type": "text", "text": msg}]

    if kind == "set_pref":
        sc = _scenario_for_user(user_id)
        sc2, note = apply_pref_to_scenario(
            sc,
            worker=intent.get("worker"),
            day=intent.get("day"),
        )
        _USER_SCENARIOS[user_id] = sc2
        result = run_shift_for_line(sc2)
        return [
            {"type": "text", "text": note},
            build_shift_flex(result, alt_text=note),
        ]

    # show_shift（既定）
    sc = _scenario_for_user(user_id)
    result = run_shift_for_line(sc)
    return [
        {"type": "text", "text": "今週のシフトたたき台です。"},
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
                    "text": "テキストで「シフト見せて」または「希望休 日曜」と送ってください。",
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
            "endpoints": {"webhook": "POST /webhook", "health": "GET /health"},
            "note": (
                "資格情報未設定時はデモモードです。"
                "実 LINE への送受信は Channel secret/token 設定後のみ。"
            ),
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "status": connection_status()})


@app.post("/webhook")
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature")
    creds = get_line_credentials()
    status = connection_status(creds)
    secret = creds.get("channel_secret") or ""

    # 署名検証: secret があるときだけ必須。デモ／未設定はスキップして受理。
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

    # LINE プラットフォームへは常に 200 を返す（検証・再送回避）
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
    """資格情報なしのローカル確認用。本文 JSON: {"text":"シフト見せて","userId":"Udemo"}"""
    data = request.get_json(silent=True) or {}
    text = data.get("text") or "シフト見せて"
    user_id = data.get("userId") or "Udemo"
    messages = handle_text_message(user_id, text)
    # プレビュー用にテキスト版も付ける
    preview_text = None
    for m in messages:
        if m.get("type") == "flex":
            # 同じシナリオでテキストも生成
            sc = _scenario_for_user(user_id)
            preview_text = build_shift_text(run_shift_for_line(sc))
            break
        if m.get("type") == "text" and preview_text is None:
            preview_text = m.get("text")
    return jsonify(
        {
            "ok": True,
            "status": connection_status(),
            "input": {"text": text, "userId": user_id},
            "messages": messages,
            "preview_text": preview_text,
        }
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("LINE_WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("LINE_WEBHOOK_PORT", "8080"))
    status = connection_status()
    LOG.info("starting LINE webhook on %s:%s status=%s", host, port, status)
    if status != "接続済":
        LOG.info(
            "デモ／モックモードです。実 LINE には接続していません。"
            " LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。"
        )
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
