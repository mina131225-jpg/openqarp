#!/usr/bin/env python3
"""Abstracted Payment Provider for store subscriptions.

Never uses LINE Mini App IAP for subscriptions (consumable-only).
Default: MockPaymentProvider (local demo checkout).
Optional: StripeCheckoutProvider when STRIPE_SECRET_KEY is set.

Flow:
  create_checkout_session(store_id, plan, ...) → checkout_url
  user pays on external page
  provider calls success webhook → attach plan to store_id
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from billing import PLAN_CATALOG, PLAN_FREE, normalize_plan_code

# In-process session store for mock + pending checkouts (PoC).
_SESSIONS: dict[str, dict[str, Any]] = {}


def public_base_url() -> str:
    base = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("LINE_PUBLIC_BASE_URL") or "").strip()
    if base:
        return base.rstrip("/")
    host = os.environ.get("LINE_WEBHOOK_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = os.environ.get("LINE_WEBHOOK_PORT", "8080")
    return f"http://{host}:{port}"


DEV_WEBHOOK_SECRET = "dev-payment-webhook-secret"


def webhook_secret() -> str:
    return (os.environ.get("PAYMENT_WEBHOOK_SECRET") or DEV_WEBHOOK_SECRET).strip()


def webhook_secret_is_dev_default() -> bool:
    """PAYMENT_WEBHOOK_SECRET 未設定（公開リポジトリ上の既定値）なら True。"""
    return webhook_secret() == DEV_WEBHOOK_SECRET


# ---- モック決済のワンタイム署名トークン -------------------------------------
# 店長の LINE リクエストで作られた checkout セッションにだけ紐づく。
# 形式: "<exp>.<nonce>.<hmac>"  hmac = HMAC-SHA256(key, session_id|store_id|plan|exp|nonce)
# 有効期限 15 分・1回限り（使用済み nonce はセッションに記録）。
MOCK_PAY_TOKEN_TTL_SEC = 15 * 60
_PROCESS_TOKEN_KEY = secrets.token_bytes(32)


def _mock_token_key() -> bytes:
    env = (os.environ.get("MOCK_PAY_TOKEN_SECRET") or "").strip()
    return env.encode("utf-8") if env else _PROCESS_TOKEN_KEY


def _mock_token_mac(sess: dict[str, Any], exp: int, nonce: str) -> str:
    msg = "|".join([str(sess.get("session_id")), str(sess.get("store_id")), str(sess.get("plan")), str(exp), nonce])
    return hmac.new(_mock_token_key(), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_mock_pay_token(session_id: str, *, now: float | None = None) -> str:
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise KeyError("session not found")
    exp = int((now if now is not None else time.time()) + MOCK_PAY_TOKEN_TTL_SEC)
    nonce = secrets.token_urlsafe(12)
    sess.setdefault("meta", {})["pay_nonce"] = nonce
    sess["meta"]["pay_token_exp"] = exp
    sess["meta"]["pay_token_used"] = False
    return f"{exp}.{nonce}.{_mock_token_mac(sess, exp, nonce)}"


def check_mock_pay_token(session_id: str, token: str | None, *, now: float | None = None) -> str | None:
    """None=有効。それ以外は拒否理由（expired / used / invalid / session）。消費はしない。"""
    sess = _SESSIONS.get(session_id or "")
    if not sess:
        return "session"
    try:
        exp_s, nonce, mac = (token or "").split(".", 2)
        exp = int(exp_s)
    except ValueError:
        return "invalid"
    meta = sess.get("meta") or {}
    if not hmac.compare_digest(_mock_token_mac(sess, exp, nonce), mac):
        return "invalid"
    if nonce != meta.get("pay_nonce"):
        return "invalid"
    if meta.get("pay_token_used") or sess.get("status") == "completed":
        return "used"
    if (now if now is not None else time.time()) > exp:
        return "expired"
    return None


def consume_mock_pay_token(session_id: str, token: str | None, *, now: float | None = None) -> str | None:
    err = check_mock_pay_token(session_id, token, now=now)
    if err is None:
        _SESSIONS[session_id]["meta"]["pay_token_used"] = True
    return err


def sign_payload(body: bytes, *, secret: str | None = None) -> str:
    key = (secret or webhook_secret()).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str | None, *, secret: str | None = None) -> bool:
    if not signature:
        return False
    expected = sign_payload(body, secret=secret)
    return hmac.compare_digest(expected, signature.strip())


@dataclass
class CheckoutSession:
    session_id: str
    store_id: str
    plan: str
    user_id: str | None = None
    amount_yen: int = 0
    status: str = "open"  # open|completed|expired
    checkout_url: str = ""
    success_url: str = ""
    cancel_url: str = ""
    customer_id: str | None = None
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "store_id": self.store_id,
            "plan": self.plan,
            "user_id": self.user_id,
            "amount_yen": self.amount_yen,
            "status": self.status,
            "checkout_url": self.checkout_url,
            "success_url": self.success_url,
            "cancel_url": self.cancel_url,
            "customer_id": self.customer_id,
            "created_at": self.created_at,
            "meta": self.meta,
        }


class PaymentProvider(ABC):
    """Provider-agnostic checkout + webhook contract."""

    name: str = "abstract"

    @abstractmethod
    def create_checkout_session(
        self,
        *,
        store_id: str,
        plan: str,
        user_id: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> CheckoutSession:
        ...

    @abstractmethod
    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        """Return normalized event: {type, store_id, plan, customer_id, session_id, status}."""
        ...

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return _SESSIONS.get(session_id)


class MockPaymentProvider(PaymentProvider):
    """Local demo provider — no real money. Completes via /billing/mock-pay."""

    name = "mock"

    def create_checkout_session(
        self,
        *,
        store_id: str,
        plan: str,
        user_id: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> CheckoutSession:
        plan_c = normalize_plan_code(plan)
        if plan_c == PLAN_FREE:
            raise ValueError("FREE plan does not require checkout")
        amount = int(PLAN_CATALOG[plan_c]["price_yen_month"])
        sid = "cs_mock_" + secrets.token_hex(8)
        base = public_base_url()
        success = success_url or f"{base}/billing/success?session_id={sid}"
        cancel = cancel_url or f"{base}/billing/cancel?session_id={sid}"
        checkout = f"{base}/billing/checkout?session_id={sid}"
        cust = "cus_mock_" + hashlib.sha256((store_id or "").encode()).hexdigest()[:10]
        sess = CheckoutSession(
            session_id=sid,
            store_id=store_id,
            plan=plan_c,
            user_id=user_id,
            amount_yen=amount,
            checkout_url=checkout,
            success_url=success,
            cancel_url=cancel,
            customer_id=cust,
            meta={"provider": self.name},
        )
        _SESSIONS[sid] = sess.to_dict()
        token = issue_mock_pay_token(sid)
        sess.checkout_url = checkout + "&t=" + urllib.parse.quote(token, safe="")
        _SESSIONS[sid]["checkout_url"] = sess.checkout_url
        return sess

    def complete_session(self, session_id: str) -> dict[str, Any]:
        sess = _SESSIONS.get(session_id)
        if not sess:
            raise KeyError("session not found")
        if sess.get("status") == "completed":
            return sess
        sess["status"] = "completed"
        sess["completed_at"] = time.time()
        _SESSIONS[session_id] = sess
        return sess

    def build_success_webhook_body(self, session_id: str) -> bytes:
        sess = self.complete_session(session_id)
        payload = {
            "type": "checkout.session.completed",
            "provider": self.name,
            "session_id": session_id,
            "store_id": sess["store_id"],
            "plan": sess["plan"],
            "customer_id": sess.get("customer_id"),
            "amount_yen": sess.get("amount_yen"),
            "status": "active",
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        sig = headers.get("X-Payment-Signature") or headers.get("x-payment-signature")
        if not verify_signature(body, sig):
            raise PermissionError("invalid payment webhook signature")
        data = json.loads(body.decode("utf-8") or "{}")
        return {
            "type": data.get("type") or "checkout.session.completed",
            "store_id": data.get("store_id"),
            "plan": normalize_plan_code(data.get("plan")),
            "customer_id": data.get("customer_id"),
            "session_id": data.get("session_id"),
            "status": data.get("status") or "active",
            "provider": data.get("provider") or self.name,
            "raw": data,
        }


class StripeCheckoutProvider(PaymentProvider):
    """Stripe Checkout skeleton. Activates when STRIPE_SECRET_KEY is set.

    Human setup still required: Products/Prices for STANDARD/PRO, webhook endpoint
    pointing to POST /billing/webhook with checkout.session.completed.
    """

    name = "stripe"

    def __init__(self) -> None:
        self.secret = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
        self.price_standard = (os.environ.get("STRIPE_PRICE_STANDARD") or "").strip()
        self.price_pro = (os.environ.get("STRIPE_PRICE_PRO") or "").strip()
        self.whsec = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()

    def _price_for(self, plan: str) -> str:
        plan_c = normalize_plan_code(plan)
        if plan_c == "STANDARD":
            return self.price_standard
        if plan_c == "PRO":
            return self.price_pro
        raise ValueError(f"no Stripe price for plan {plan_c}")

    def create_checkout_session(
        self,
        *,
        store_id: str,
        plan: str,
        user_id: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> CheckoutSession:
        if not self.secret:
            raise RuntimeError("STRIPE_SECRET_KEY not set")
        plan_c = normalize_plan_code(plan)
        price = self._price_for(plan_c)
        if not price:
            raise RuntimeError(f"Stripe price id missing for {plan_c}")
        base = public_base_url()
        success = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
        cancel = cancel_url or f"{base}/billing/cancel"
        # Lazy import — optional dependency
        try:
            import urllib.request
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("urllib unavailable") from exc

        body = urllib.parse.urlencode(
            {
                "mode": "subscription",
                "success_url": success,
                "cancel_url": cancel,
                "line_items[0][price]": price,
                "line_items[0][quantity]": 1,
                "client_reference_id": store_id,
                "metadata[store_id]": store_id,
                "metadata[plan]": plan_c,
                "metadata[user_id]": user_id or "",
            }
        ).encode()
        req = urllib.request.Request(
            "https://api.stripe.com/v1/checkout/sessions",
            data=body,
            headers={"Authorization": f"Bearer {self.secret}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        sid = data["id"]
        sess = CheckoutSession(
            session_id=sid,
            store_id=store_id,
            plan=plan_c,
            user_id=user_id,
            amount_yen=int(PLAN_CATALOG[plan_c]["price_yen_month"]),
            checkout_url=data.get("url") or "",
            success_url=success,
            cancel_url=cancel,
            customer_id=data.get("customer"),
            meta={"provider": self.name, "stripe": {"id": sid}},
        )
        _SESSIONS[sid] = sess.to_dict()
        return sess

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        # Prefer Stripe signature when configured; else shared PAYMENT_WEBHOOK_SECRET.
        stripe_sig = headers.get("Stripe-Signature") or headers.get("stripe-signature")
        if self.whsec and stripe_sig:
            # Minimal verification: require presence; full Stripe sig verify needs stripe lib.
            # For PoC we also accept our HMAC if X-Payment-Signature present.
            pass
        our_sig = headers.get("X-Payment-Signature") or headers.get("x-payment-signature")
        if our_sig and not verify_signature(body, our_sig):
            raise PermissionError("invalid payment webhook signature")
        if not our_sig and not (self.whsec and stripe_sig):
            # Allow unsigned only in explicit demo
            if (os.environ.get("PAYMENT_ALLOW_UNSIGNED_WEBHOOK") or "").lower() not in {
                "1", "true", "yes",
            }:
                raise PermissionError("missing payment webhook signature")

        data = json.loads(body.decode("utf-8") or "{}")
        # Stripe-shaped or our normalized shape
        if data.get("type") == "checkout.session.completed" and "data" in data:
            obj = (data.get("data") or {}).get("object") or {}
            meta = obj.get("metadata") or {}
            return {
                "type": "checkout.session.completed",
                "store_id": meta.get("store_id") or obj.get("client_reference_id"),
                "plan": normalize_plan_code(meta.get("plan")),
                "customer_id": obj.get("customer"),
                "session_id": obj.get("id"),
                "status": "active",
                "provider": self.name,
                "raw": data,
            }
        return {
            "type": data.get("type") or "checkout.session.completed",
            "store_id": data.get("store_id"),
            "plan": normalize_plan_code(data.get("plan")),
            "customer_id": data.get("customer_id"),
            "session_id": data.get("session_id"),
            "status": data.get("status") or "active",
            "provider": data.get("provider") or self.name,
            "raw": data,
        }


def get_payment_provider() -> PaymentProvider:
    preferred = (os.environ.get("PAYMENT_PROVIDER") or "").strip().lower()
    if preferred == "stripe" or (not preferred and os.environ.get("STRIPE_SECRET_KEY")):
        return StripeCheckoutProvider()
    return MockPaymentProvider()


def period_end_iso(months: int = 1) -> str:
    now = datetime.now(timezone.utc).astimezone()
    # approximate month
    return (now + timedelta(days=30 * months)).isoformat(timespec="seconds")
