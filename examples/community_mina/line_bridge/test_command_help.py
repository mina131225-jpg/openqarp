#!/usr/bin/env python3
"""Unknown / near-miss / role-aware help."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _blob(msgs) -> str:
    return json.dumps(msgs, ensure_ascii=False)


def _has_qr_label(msgs, label: str) -> bool:
    blob = _blob(msgs)
    return label in blob


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_help_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ["LINE_DEMO_MODE"] = "true"
    os.environ["PAYMENT_PROVIDER"] = "mock"
    import importlib
    import command_help as ch
    import shift_messages as sm
    import stores
    import webhook_app as wh

    for m in (ch, sm, stores, wh):
        importlib.reload(m)

    # --- unit: normalize + suggest ---
    _assert(ch.normalize_for_match("シフト見せてよ") == "シフト見せて", "strip よ")
    _assert(ch.normalize_for_match("登緑") == "登録", "typo 登緑")
    _assert(ch.normalize_for_match("申し込むプロ") == "申し込む プロ", "insert space")
    _assert(ch.suggest_command("登緑", role="guest") == "登録 DEMO01" or ch.suggest_command("登緑", role="guest") == "登録", 
            f"suggest 登緑 → {ch.suggest_command('登緑', role='guest')}")
    # canonical for 登録 aliases is "登録 DEMO01"
    _assert(ch.suggest_command("ヘルフ", role="staff") == "使い方", f"ヘルフ → {ch.suggest_command('ヘルフ', role='staff')}")
    _assert(ch.suggest_command("出金", role="staff") == "出勤", "出金→出勤")
    _assert(ch.suggest_command("シフト見せてよ", role="staff") in {"シフト見せて", "自分のシフト"}, 
            f"見せてよ → {ch.suggest_command('シフト見せてよ', role='staff')}")
    _assert(ch.suggest_command("asdfghjklzzz", role="staff") is None, "gibberish no suggest")
    _assert(ch.suggest_command("めにゅー", role="manager") == "メニュー", "めにゅー")

    # parse: unknown vs help
    _assert(sm.parse_user_intent("あいうえおかきくけこ").get("intent") == "unknown", "gibberish unknown")
    _assert(sm.parse_user_intent("ヘルプ").get("intent") == "help", "ヘルプ")
    _assert(sm.parse_user_intent("使い方").get("intent") == "help", "使い方")
    _assert(sm.parse_user_intent("？").get("intent") == "help", "？")
    _assert(sm.parse_user_intent("?").get("intent") == "help", "?")

    # Note: シフト見せてよ still matches show_shift via substring — that's fine
    _assert(sm.parse_user_intent("シフト見せてよ").get("intent") == "show_shift", "見せてよ still works")

    handle = wh.handle_text_message

    # --- unregistered ---
    msgs = handle("U_help_guest", "あああああxyz")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("すみません" in body or "分かりません" in body, body[:300])
    _assert("未登録" in body or "登録" in body, body[:400])
    _assert("店舗作成" in body or "お店を始める" in body, body[:400])
    _assert(_has_qr_label(msgs, "使い方") or _has_qr_label(msgs, "メニュー"), "guest qr help/menu")
    # must NOT dump other-store invite codes or wage tables
    _assert("invite_code" not in body.lower() or "DEMO01" in body, "ok to mention DEMO01 example")

    msgs = handle("U_help_guest", "登緑")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("もしかして" in body, body[:400])
    _assert("登録" in body, body[:400])

    msgs = handle("U_help_guest", "ヘルプ")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("使い方" in body or "登録" in body, body[:400])
    _assert("未登録" in body or "店舗に登録" in body or "お店を始める" in body, body[:500])
    _assert("シフト3案" not in body or "店長" not in body.split("スタッフ")[0], "guest help is guest-scoped")

    # --- manager + staff ---
    ok, msg, store = stores.create_store_as_manager("U_help_mgr", "ヘルプ店", display_name="店長ヘルプ")
    _assert(ok, msg)
    sid = store["store_id"]
    handle("U_help_mgr", "上記を確認しました")
    handle("U_help_mgr", "同意する")
    inv = stores.get_store(sid)["invite_code"]
    handle("U_help_staff", f"登録 {inv} 花子")

    # isolation: guest unknown must not show this store's invite in suggestions beyond own flow
    # staff unknown
    msgs = handle("U_help_staff", "xyz不明コマンド999")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("すみません" in body, body[:300])
    _assert("スタッフ" in body, body[:400])
    _assert("出勤" in body and "退勤" in body, body[:500])
    _assert("条件で自動作成" not in body and "店舗設定" not in body, "staff must not see manager-only cmds")
    _assert(_has_qr_label(msgs, "使い方") or _has_qr_label(msgs, "メニュー"), "staff qr")
    # other store's name must not appear — only own store
    other_ok, _, other = stores.create_store_as_manager("U_other_mgr", "秘密の他店", display_name="他")
    _assert(other_ok, "other store")
    _assert("秘密の他店" not in body, "no other-store leak")

    msgs = handle("U_help_staff", "出金")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("もしかして" in body and "出勤" in body, body[:400])

    msgs = handle("U_help_staff", "使い方")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("スタッフ" in body, body[:400])
    _assert("出勤" in body and "シフト希望" in body, body[:500])
    _assert("条件設定" not in body, "staff help no manager conditions")

    # manager
    msgs = handle("U_help_mgr", "なにこれわからない")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("すみません" in body, body[:300])
    _assert("店長" in body, body[:400])
    _assert("シフト作成" in body or "条件設定" in body, body[:500])
    _assert("秘密の他店" not in body, "mgr unknown no other store")
    _assert(_has_qr_label(msgs, "使い方") and _has_qr_label(msgs, "メニュー"), "mgr qr menu+help")

    msgs = handle("U_help_mgr", "ヘルフ")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("もしかして" in body and "使い方" in body, body[:400])

    msgs = handle("U_help_mgr", "？")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("店長メニュー" in body or "店長" in body, body[:500])
    _assert("条件設定" in body or "シフト作成" in body, body[:500])
    _assert("ヘルプ店" in body, "own store name ok")

    # pending invite-code input: gibberish is NOT a command → keep waiting (re-prompt)
    stores.set_user_state("U_pending_cmd", {"kind": "await_invite_code"})
    msgs = handle("U_pending_cmd", "わけわからない入力")
    st = stores.get_user_state("U_pending_cmd")
    _assert(st and st.get("kind") == "await_invite_code", f"pending invite kept: {st}")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("招待コード" in body, body[:300])
    # explicit ヘルプ cancels pending and shows help
    stores.set_user_state("U_pending_cmd", {"kind": "await_invite_code"})
    msgs = handle("U_pending_cmd", "ヘルプ")
    _assert(stores.get_user_state("U_pending_cmd") is None, "help clears pending")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("使い方" in body or "登録" in body, body[:400])

    print("[help] ALL ASSERTIONS PASSED")
    # sample dumps for the report
    print("---SAMPLE guest gibberish---")
    print("\n".join(m.get("text") or "" for m in handle("U_samp_g", "あいうxyz"))[:500])
    print("---SAMPLE staff near-miss---")
    print("\n".join(m.get("text") or "" for m in handle("U_help_staff", "出金"))[:500])
    print("---SAMPLE manager help---")
    print("\n".join(m.get("text") or "" for m in handle("U_help_mgr", "ヘルプ"))[:700])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
