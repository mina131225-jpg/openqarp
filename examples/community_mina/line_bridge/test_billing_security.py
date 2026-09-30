#!/usr/bin/env python3
"""モック決済・課金 webhook のロックダウン。

- /billing/mock-pay・/billing/checkout: ローカル直 or X-Admin-Token、
  またはトンネル経由なら「店長の LINE 申し込みで発行された 15分・1回限りの署名トークン」のみ
- /billing/webhook: PAYMENT_WEBHOOK_SECRET 未設定（既定の開発用）ならローカル直 or 管理トークンのみ
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import traceback
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

TUNNEL = {"Cf-Connecting-Ip": "203.0.113.9", "Cf-Ray": "8abc-NRT"}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _urls(msgs) -> list[str]:
    import json
    blob = json.dumps(msgs, ensure_ascii=False)
    return re.findall(r"https?://[^\s\"\\]+/billing/checkout\?session_id=[^\s\"\\]+", blob)


def _q(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_bsec_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ["LINE_DEMO_MODE"] = "true"
    os.environ["PAYMENT_PROVIDER"] = "mock"
    os.environ["PUBLIC_BASE_URL"] = "https://example.trycloudflare.com"
    os.environ.pop("PAYMENT_WEBHOOK_SECRET", None)
    os.environ.pop("LINE_STORES_ADMIN_TOKEN", None)
    os.environ.pop("MOCK_PAY_TOKEN_SECRET", None)
    os.environ["LINE_DEMO_ADMIN_TOKEN"] = "adm-for-tests-only"
    import importlib
    import billing, payment_provider as pp, stores, webhook_app as wh  # noqa: E401
    for m in (billing, pp, stores, wh):
        importlib.reload(m)
    c = wh.app.test_client()
    handle = wh.handle_text_message

    ok, msg, store = stores.create_store_as_manager("U_bs_mgr", "決済テスト店", display_name="店長")
    _assert(ok, msg)
    sid = store["store_id"]
    handle("U_bs_mgr", "上記を確認しました")
    handle("U_bs_mgr", "同意する")
    inv = stores.get_store(sid)["invite_code"]
    handle("U_bs_staff", f"登録 {inv}")

    # staff cannot obtain a checkout link
    smsgs = handle("U_bs_staff", "申し込む プロ")
    _assert(not _urls(smsgs), "staff gets no checkout url")
    _assert(not _urls(handle("U_bs_staff", "プラン")), "staff plan view has no checkout url")

    # manager LINE request → signed one-time link
    urls = _urls(handle("U_bs_mgr", "申し込む プロ"))
    _assert(urls, "manager gets checkout url")
    url = urls[0]
    q = _q(url)
    sess_id, tok = q["session_id"], q.get("t", "")
    _assert(tok.count(".") == 2, "token present in link")
    path = "/billing/checkout?" + urllib.parse.urlencode({"session_id": sess_id, "t": tok})

    # --- tunnel without / with bad token → 403
    _assert(c.post("/billing/mock-pay", data={"session_id": sess_id}, headers=TUNNEL).status_code == 403,
            "mock-pay via tunnel without token denied")
    _assert(c.post("/billing/mock-pay", data={"session_id": sess_id, "t": tok + "x"}, headers=TUNNEL).status_code == 403,
            "tampered token denied")
    exp, nonce, mac = tok.split(".")
    forged = f"{int(exp) + 3600}.{nonce}.{mac}"
    _assert(c.post("/billing/mock-pay", data={"session_id": sess_id, "t": forged}, headers=TUNNEL).status_code == 403,
            "extended expiry denied (signature bound)")
    # token bound to its session: another store's session cannot reuse it
    ok2, _, store2 = stores.create_store_as_manager("U_bs_mgr2", "他店", display_name="他店長")
    other = pp.MockPaymentProvider().create_checkout_session(store_id=store2["store_id"], plan="PRO", user_id="U_bs_mgr2")
    _assert(c.post("/billing/mock-pay", data={"session_id": other.session_id, "t": tok}, headers=TUNNEL).status_code == 403,
            "token not valid for another session")
    _assert(c.get(f"/billing/checkout?session_id={sess_id}", headers=TUNNEL).status_code == 403,
            "checkout page via tunnel without token denied")
    _assert(stores.get_store(sid).get("subscription_status") != "active" or stores.get_store(sid).get("plan") != "PRO",
            "nothing activated yet")

    # --- phone flow through tunnel with the valid token works
    r = c.get(path, headers=TUNNEL)
    _assert(r.status_code == 200, f"checkout page with token: {r.status_code}")
    html = r.get_data(as_text=True)
    _assert('name="t"' in html, "form carries token")
    r = c.post("/billing/mock-pay", data={"session_id": sess_id, "t": tok}, headers=TUNNEL)
    _assert(r.status_code == 302, f"mock-pay with token succeeds: {r.status_code}")
    s = stores.get_store(sid)
    _assert(s.get("plan") == "PRO" and s.get("subscription_status") == "active", "PRO activated")
    # single use
    r = c.post("/billing/mock-pay", data={"session_id": sess_id, "t": tok}, headers=TUNNEL)
    _assert(r.status_code == 403 and "使用済み" in r.get_data(as_text=True), "token single-use")
    _assert(c.get(path, headers=TUNNEL).status_code == 403, "used token cannot reopen checkout")

    # expiry (15 min)
    sess2 = pp.MockPaymentProvider().create_checkout_session(store_id=sid, plan="STANDARD", user_id="U_bs_mgr")
    tok2 = _q(sess2.checkout_url)["t"]
    _assert(pp.check_mock_pay_token(sess2.session_id, tok2) is None, "fresh token ok")
    _assert(pp.check_mock_pay_token(sess2.session_id, tok2, now=time.time() + 14 * 60) is None, "ok at 14 min")
    _assert(pp.check_mock_pay_token(sess2.session_id, tok2, now=time.time() + 16 * 60) == "expired", "expired at 16 min")
    real_time = pp.time.time
    try:
        pp.time.time = lambda: real_time() + 16 * 60
        r = c.post("/billing/mock-pay", data={"session_id": sess2.session_id, "t": tok2}, headers=TUNNEL)
        _assert(r.status_code == 403 and "有効期限" in r.get_data(as_text=True), "expired token denied via tunnel")
    finally:
        pp.time.time = real_time

    # --- local direct & admin token still allowed (dev tooling)
    sess3 = pp.MockPaymentProvider().create_checkout_session(store_id=sid, plan="STANDARD", user_id="U_bs_mgr")
    _assert(c.post("/billing/mock-pay", data={"session_id": sess3.session_id}).status_code == 302, "local direct allowed")
    sess4 = pp.MockPaymentProvider().create_checkout_session(store_id=sid, plan="PRO", user_id="U_bs_mgr")
    r = c.post("/billing/mock-pay", data={"session_id": sess4.session_id},
               headers={**TUNNEL, "X-Admin-Token": "adm-for-tests-only"})
    _assert(r.status_code == 302, "admin token allowed through tunnel")
    r = c.post("/billing/mock-pay", data={"session_id": sess4.session_id},
               headers={**TUNNEL, "X-Admin-Token": "wrong"})
    _assert(r.status_code == 403, "wrong admin token denied")

    # --- /billing/webhook with the public dev secret: tunnel → 403, local ok
    victim = store2["store_id"]
    s5 = pp.MockPaymentProvider().create_checkout_session(store_id=victim, plan="PRO", user_id="x")
    body = pp.MockPaymentProvider().build_success_webhook_body(s5.session_id)
    sig = pp.sign_payload(body)
    r = c.post("/billing/webhook", data=body, headers={**TUNNEL, "Content-Type": "application/json", "X-Payment-Signature": sig})
    _assert(r.status_code == 403, f"forged webhook via tunnel denied: {r.status_code}")
    _assert(stores.get_store(victim).get("plan") != "PRO" or stores.get_store(victim).get("subscription_status") != "active",
            "victim store not upgraded")
    r = c.post("/billing/webhook", data=body, headers={"Content-Type": "application/json", "X-Payment-Signature": sig})
    _assert(r.status_code == 200, "local webhook ok")
    # with a real secret configured, external signed webhooks are accepted (real provider path)
    os.environ["PAYMENT_WEBHOOK_SECRET"] = "whsec-test-only"
    sig2 = pp.sign_payload(body)
    r = c.post("/billing/webhook", data=body, headers={**TUNNEL, "Content-Type": "application/json", "X-Payment-Signature": sig2})
    _assert(r.status_code == 200, "configured secret → external webhook accepted")
    r = c.post("/billing/webhook", data=body, headers={**TUNNEL, "Content-Type": "application/json", "X-Payment-Signature": sig})
    _assert(r.status_code == 403, "old dev signature rejected once secret set")
    os.environ.pop("PAYMENT_WEBHOOK_SECRET", None)

    # public pages stay public
    _assert(c.get("/billing/cancel", headers=TUNNEL).status_code == 200, "cancel public")
    _assert(c.get("/health", headers=TUNNEL).status_code == 200, "health public")
    print("[bsec] ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
