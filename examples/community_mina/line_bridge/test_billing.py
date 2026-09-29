#!/usr/bin/env python3
"""Billing / plan gate regression (temp stores.json)."""

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


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_bill_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ.pop("LINE_STORE_OWNER_CODE", None)
    os.environ["PAYMENT_PROVIDER"] = "mock"
    os.environ["PAYMENT_WEBHOOK_SECRET"] = "test-secret"
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8080"

    import importlib
    import billing
    import payment_provider as pp
    import stores
    import webhook_app as wh

    importlib.reload(billing)
    importlib.reload(pp)
    importlib.reload(stores)
    importlib.reload(wh)

    handle = wh.handle_text_message
    print(f"[bill] temp db: {tmp.name}")

    ok, msg, store = stores.create_store_as_manager("U_bill_mgr", "課金店", display_name="店長課金")
    _assert(ok and store, msg)
    sid = store["store_id"]
    # consent
    handle("U_bill_mgr", "上記を確認しました")
    handle("U_bill_mgr", "同意する")
    store = stores.get_store(sid)
    _assert(store.get("subscription_status") == "trialing", store.get("subscription_status"))
    _assert(store.get("plan") == "STANDARD", store.get("plan"))
    _assert(billing.days_remaining(store) is not None and billing.days_remaining(store) > 0, "trial days")
    _assert(billing.has_feature(store, "three_plans"), "trial has three_plans")
    _assert(billing.has_feature(store, "payroll"), "trial has payroll")

    # show plans
    msgs = handle("U_bill_mgr", "プラン")
    body = "\n".join((m.get("text") or "") for m in msgs)
    _assert("料金プラン" in body or "スタンダード" in body, body[:400])

    # expire → FREE features, data kept
    stores.set_store_subscription(sid, subscription_status="expired")
    store = stores.get_store(sid)
    _assert(billing.effective_plan(store) == "FREE", billing.effective_plan(store))
    _assert(not billing.has_feature(store, "three_plans"), "expired loses three_plans")
    msgs = handle("U_bill_mgr", "シフト3案作って")
    body = "\n".join((m.get("text") or "") for m in msgs)
    _assert("プラン" in body or "スタンダード" in body or "機能" in body, body[:500])
    # data still there
    store2 = stores.get_store(sid)
    _assert(store2 and store2.get("store_name") == "課金店", "store retained")
    _assert(any(m.get("user_id") == "U_bill_mgr" for m in store2.get("members") or []), "member retained")

    # checkout + webhook activates PRO
    provider = pp.MockPaymentProvider()
    sess = provider.create_checkout_session(store_id=sid, plan="PRO", user_id="U_bill_mgr")
    body_bytes = provider.build_success_webhook_body(sess.session_id)
    sig = pp.sign_payload(body_bytes, secret="test-secret")
    result, status = wh._apply_payment_webhook(body_bytes, {"X-Payment-Signature": sig})
    _assert(status == 200 and result.get("ok"), result)
    store = stores.get_store(sid)
    _assert(store.get("plan") == "PRO", store.get("plan"))
    _assert(store.get("subscription_status") == "active", store.get("subscription_status"))
    _assert(store.get("payment_customer_id"), "customer id set")
    _assert(billing.has_feature(store, "qaoa_compare"), "pro qaoa")
    _assert(billing.has_feature(store, "multi_store"), "pro multi")

    # staff cap on FREE
    stores.set_store_subscription(sid, plan="FREE", subscription_status="none", current_period_end=None)
    store = stores.get_store(sid)
    # register up to cap
    code = store["invite_code"]
    for i in range(1, 6):
        ok, note, _ = stores.register_user(f"U_staff_{i}", code, display_name=f"スタッフ{i}")
        # manager already counts; first few OK until cap
    store = stores.get_store(sid)
    n = len(store.get("members") or [])
    _assert(n <= billing.max_staff_for(store), f"staff {n} > cap")
    # one more should fail if at cap
    if n >= billing.max_staff_for(store):
        ok, note, _ = stores.register_user("U_overflow", code, display_name="溢れた")
        _assert(not ok and "上限" in note, note)

    # isolation still: another store
    ok, _, store_b = stores.create_store_as_manager("U_bill_mgr_b", "別店", display_name="店長B")
    _assert(ok and store_b, "second manager store")
    _assert(store_b["store_id"] != sid, "ids differ")
    sa = stores.get_store_for_user("U_bill_mgr")
    sb = stores.get_store_for_user("U_bill_mgr_b")
    _assert(sa["store_id"] == sid and sb["store_id"] == store_b["store_id"], "isolation")

    print("[bill] ALL ASSERTIONS PASSED")
    Path(tmp.name).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
