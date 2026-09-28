#!/usr/bin/env python3
"""LINE へ週次シフト表をプッシュ（またはデモログ）するユーティリティ。

使い方:
  # デモ（トークン無し）— ペイロードを標準出力へ
  python notify.py --demo

  # 実送信（要 LINE_CHANNEL_ACCESS_TOKEN + LINE_USER_ID）
  export LINE_CHANNEL_ACCESS_TOKEN=...
  export LINE_USER_ID=Uxxxxxxxx
  python notify.py --flex

実 LINE 送信は資格情報がある場合のみ。未設定時は決して API を叩かない。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from shift_messages import (  # noqa: E402
    build_shift_flex,
    build_shift_text,
    connection_status,
    get_line_credentials,
    load_base_scenario,
    run_shift_for_line,
)

LOG = logging.getLogger("line_bridge.notify")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


def _demo_forced() -> bool:
    return os.environ.get("LINE_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_live_ready(creds: dict[str, str] | None = None) -> bool:
    """プッシュ可能な状態か（token + userId、かつデモ強制でない）。"""
    c = creds or get_line_credentials()
    if _demo_forced():
        return False
    return bool(c.get("channel_access_token") and c.get("user_id"))


def is_reply_ready(creds: dict[str, str] | None = None) -> bool:
    c = creds or get_line_credentials()
    if _demo_forced():
        return False
    return bool(c.get("channel_access_token"))


def build_messages(
    result: dict[str, Any] | None = None,
    *,
    use_flex: bool = True,
    extra_text: str | None = None,
) -> list[dict[str, Any]]:
    """送信メッセージ配列を組み立てる。"""
    r = result or run_shift_for_line(load_base_scenario())
    messages: list[dict[str, Any]] = []
    if extra_text:
        messages.append({"type": "text", "text": extra_text[:5000]})
    if use_flex:
        messages.append(build_shift_flex(r))
    else:
        messages.append({"type": "text", "text": build_shift_text(r)[:5000]})
    return messages


def push_messages(
    user_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    *,
    result: dict[str, Any] | None = None,
    use_flex: bool = True,
    extra_text: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """ユーザーへプッシュ。dry_run / 資格情報不足時はログのみ。

    戻り値: {ok, mode, status_code?, body?, payload}
    """
    creds = get_line_credentials()
    uid = (user_id or creds.get("user_id") or "").strip()
    msgs = messages if messages is not None else build_messages(
        result, use_flex=use_flex, extra_text=extra_text
    )
    payload = {"to": uid or "(no-userId)", "messages": msgs}

    status = connection_status(creds)
    force_dry = dry_run if dry_run is not None else (
        status != "接続済" or not uid or _demo_forced()
    )

    if force_dry or not is_live_ready({**creds, "user_id": uid}):
        LOG.info(
            "[DEMO/MOCK push] status=%s user=%s messages=%d",
            status,
            uid or "(none)",
            len(msgs),
        )
        LOG.info("[DEMO/MOCK payload]\n%s", json.dumps(payload, ensure_ascii=False, indent=2))
        return {
            "ok": True,
            "mode": "demo",
            "status": status,
            "payload": payload,
            "sent": False,
            "detail": "資格情報不足またはデモモードのため実送信していません。",
        }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {creds['channel_access_token']}",
    }
    try:
        resp = requests.post(
            LINE_PUSH_URL,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=20,
        )
    except requests.RequestException as exc:
        LOG.error("LINE push failed: %s", exc)
        return {
            "ok": False,
            "mode": "live",
            "status": status,
            "payload": payload,
            "sent": False,
            "detail": str(exc),
        }

    ok = 200 <= resp.status_code < 300
    return {
        "ok": ok,
        "mode": "live",
        "status": status,
        "status_code": resp.status_code,
        "body": resp.text,
        "payload": payload,
        "sent": ok,
        "detail": "送信成功" if ok else f"送信失敗 HTTP {resp.status_code}",
    }


def reply_messages(
    reply_token: str,
    messages: list[dict[str, Any]],
    *,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """webhook の replyToken で返信。"""
    creds = get_line_credentials()
    payload = {"replyToken": reply_token, "messages": messages}
    status = connection_status(creds)
    force_dry = dry_run if dry_run is not None else (
        not is_reply_ready(creds) or _demo_forced()
    )

    if force_dry:
        LOG.info(
            "[DEMO/MOCK reply] status=%s token=%s… messages=%d",
            status,
            (reply_token or "")[:8],
            len(messages),
        )
        LOG.info("[DEMO/MOCK payload]\n%s", json.dumps(payload, ensure_ascii=False, indent=2))
        return {
            "ok": True,
            "mode": "demo",
            "status": status,
            "payload": payload,
            "sent": False,
            "detail": "デモモードのため reply API を呼んでいません。",
        }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {creds['channel_access_token']}",
    }
    try:
        resp = requests.post(
            LINE_REPLY_URL,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=20,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "mode": "live",
            "status": status,
            "payload": payload,
            "sent": False,
            "detail": str(exc),
        }

    ok = 200 <= resp.status_code < 300
    return {
        "ok": ok,
        "mode": "live",
        "status": status,
        "status_code": resp.status_code,
        "body": resp.text,
        "payload": payload,
        "sent": ok,
        "detail": "返信成功" if ok else f"返信失敗 HTTP {resp.status_code}",
    }


def broadcast_to_store(
    store: dict,
    messages: list[dict[str, Any]] | None = None,
    *,
    result: dict[str, Any] | None = None,
    use_flex: bool = True,
    extra_text: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """店舗の全 line_user_ids へプッシュ（デモ時はペイロード列挙のみ）。

    戻り値: {ok, mode, targets, results, sent_count, detail}
    """
    uids = list(store.get("line_user_ids") or [])
    store_name = store.get("store_name") or store.get("store_id") or "(store)"
    msgs = messages if messages is not None else build_messages(
        result, use_flex=use_flex, extra_text=extra_text
    )
    if not uids:
        LOG.info("[DEMO/MOCK broadcast] store=%s no members", store_name)
        return {
            "ok": True,
            "mode": "demo",
            "targets": [],
            "results": [],
            "sent_count": 0,
            "detail": f"「{store_name}」に登録メンバーがいません（プレビューのみ）。",
            "messages": msgs,
        }

    results = []
    sent_count = 0
    modes = set()
    for uid in uids:
        out = push_messages(
            user_id=uid,
            messages=msgs,
            dry_run=dry_run,
        )
        modes.add(out.get("mode") or "demo")
        if out.get("sent"):
            sent_count += 1
        results.append(
            {
                "user_id": uid,
                "ok": out.get("ok"),
                "sent": out.get("sent"),
                "mode": out.get("mode"),
                "detail": out.get("detail"),
            }
        )

    mode = "live" if modes == {"live"} else ("demo" if "demo" in modes else "mixed")
    detail = (
        f"「{store_name}」へ {len(uids)} 名中 {sent_count} 名に送信"
        if mode == "live"
        else f"「{store_name}」へ {len(uids)} 名分のブロードキャストをデモ生成（実送信なし）"
    )
    return {
        "ok": True,
        "mode": mode,
        "targets": uids,
        "results": results,
        "sent_count": sent_count,
        "detail": detail,
        "messages": msgs,
    }



def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="LINE へシフト表をプッシュ（デモ可）")
    p.add_argument("--demo", action="store_true", help="強制デモ（実送信しない）")
    p.add_argument("--flex", action="store_true", help="Flex Message を使う（既定）")
    p.add_argument("--text", action="store_true", help="テキストのみ")
    p.add_argument("--user-id", default="", help="送信先 userId（未指定時は env）")
    p.add_argument(
        "--extra",
        default="",
        help="表の前に付けるテキスト",
    )
    args = p.parse_args(argv)

    if args.demo:
        os.environ["LINE_DEMO_MODE"] = "true"

    use_flex = not args.text
    result = run_shift_for_line(load_base_scenario())
    out = push_messages(
        user_id=args.user_id or None,
        result=result,
        use_flex=use_flex,
        extra_text=args.extra or None,
        dry_run=True if args.demo else None,
    )
    print(json.dumps(
        {
            "ok": out["ok"],
            "mode": out["mode"],
            "status": out.get("status"),
            "sent": out.get("sent"),
            "detail": out.get("detail"),
            "message_count": len(out["payload"]["messages"]),
            "preview_text": build_shift_text(result)[:800],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
