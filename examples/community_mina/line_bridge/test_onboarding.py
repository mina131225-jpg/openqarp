#!/usr/bin/env python3
"""3分オンボーディング／招待・紹介の回帰テスト（一時 stores.json、実送信なし）。

- 友だち追加 → 店長ボタン → 店舗名 → 同意2タップ → トライアル → 招待 → サンプル3案
- スタッフ参加（ボタン／テキスト）→ 店長へ「○○さんが参加しました（n/m名）」
- 招待ページ／QR は店舗名＋招待コードのみ（他店・スタッフ・時給を漏らさない）
"""

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


def _texts(msgs: list[dict]) -> str:
    return "\n".join(m.get("text") or "" for m in msgs if m.get("type") == "text")


def _dump(msgs: list[dict]) -> str:
    return json.dumps(msgs, ensure_ascii=False)


def _actions(obj):
    if isinstance(obj, dict):
        if "action" in obj and isinstance(obj["action"], dict):
            yield obj["action"]
        for v in obj.values():
            yield from _actions(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _actions(v)


def _check_line_limits(msgs: list[dict], where: str) -> None:
    _assert(1 <= len(msgs) <= 5, f"{where}: reply must have 1..5 messages, got {len(msgs)}")
    for m in msgs:
        for item in (m.get("quickReply") or {}).get("items") or []:
            _assert(len(item["action"]["label"]) <= 20, f"{where}: quick reply label too long")
        _assert(len((m.get("quickReply") or {}).get("items") or []) <= 13, f"{where}: too many quick replies")
    for a in _actions(msgs):
        if a.get("type") == "postback":
            _assert(len(a["data"]) <= 300, f"{where}: postback data too long")
        if a.get("type") == "uri":
            _assert(len(a["uri"]) <= 1000, f"{where}: uri too long ({len(a['uri'])})")
            _assert(a["uri"].startswith("https://"), f"{where}: uri must be https")
        if "label" in a:
            _assert(len(a["label"]) <= 40, f"{where}: label too long")


def _postback_actions(msgs) -> set[str]:
    out = set()
    for a in _actions(msgs):
        if a.get("type") == "postback":
            for kv in a["data"].split("&"):
                if kv.startswith("action="):
                    out.add(kv.split("=", 1)[1])
    return out


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_onb_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ.pop("LINE_STORE_OWNER_CODE", None)
    os.environ["LINE_DEMO_MODE"] = "true"  # never call the real LINE API from tests
    os.environ["PAYMENT_PROVIDER"] = "mock"
    os.environ["PUBLIC_BASE_URL"] = "https://example-tunnel.trycloudflare.com"
    os.environ["LINE_OA_BASIC_ID"] = "@813jdxwv"

    import importlib
    import billing, invite_share, stores, onboarding, webhook_app as wh  # noqa: E401

    for mod in (billing, invite_share, stores, onboarding, wh):
        importlib.reload(mod)

    text = wh.handle_text_message
    pb = wh.handle_postback_message
    print(f"[onb] temp db: {tmp.name}")
    mgr = "U_onb_mgr"

    # 1) follow → welcome with two buttons
    w = wh.follow_messages(mgr)
    _check_line_limits(w, "follow")
    acts = _postback_actions(w)
    _assert({"onboard_manager", "onboard_staff"} <= acts, f"welcome buttons: {acts}")
    _assert("お店を始める（店長）" in _dump(w) and "スタッフとして参加" in _dump(w), "welcome labels")

    # 2) 店長 → store name (single text input) → consent gate
    r = pb(mgr, "v=1&action=onboard_manager")
    _check_line_limits(r, "ask name")
    _assert("お店の名前" in _texts(r), "asks store name")
    r = text(mgr, "青山カフェ")
    _check_line_limits(r, "store created")
    t = _texts(r)
    _assert("青山カフェ" in t and "セットアップ 1/4" in t, f"store created + progress: {t[:200]}")
    _assert("ack_terms" in _postback_actions(r), "consent ack button")
    store = stores.get_store_for_user(mgr)
    _assert(store and store["store_name"] == "青山カフェ", "store persisted")
    code, sid = store["invite_code"], store["store_id"]
    _assert(stores.get_user_state(mgr) is None, "state cleared after store creation")

    # 3) 2 taps consent → trial + invite step
    r = pb(mgr, "v=1&action=ack_terms")
    _assert("agree" in _postback_actions(r), "agree button after ack")
    r = pb(mgr, "v=1&action=agree")
    _check_line_limits(r, "agree")
    t = _texts(r)
    d = _dump(r)
    _assert("同意を記録" in t, "consent recorded")
    _assert("無料トライアル" in t and "残り14日" in t, f"trial started text: {t[:300]}")
    _assert("https://line.me/R/ti/p/@813jdxwv" in t and code in t, "invite text has friend-add URL + code")
    _assert(f"登録 {code}" in t, "invite text has one-line instruction")
    _assert("https://line.me/R/share?text=" in d, "LINE share-target button")
    _assert(f"https://example-tunnel.trycloudflare.com/invite/{code}" in d, "invite page button")
    _assert("セットアップ 2/4" in d, "progress 2/4 on invite step")
    _assert({"sample_plans", "invite_qr", "refer_service"} <= _postback_actions(r), "invite step buttons")
    _assert("resume_setup" in _postback_actions(r), "続きから quick reply")

    # QR image message
    r = pb(mgr, "v=1&action=invite_qr")
    _check_line_limits(r, "qr")
    img = r[0]
    _assert(img["type"] == "image" and img["originalContentUrl"].endswith(f"/invite/{code}/qr.png"), "image message")

    # 4) sample plans (manager alone → demo staff), clearly labelled, not confirmable
    r = pb(mgr, "v=1&action=sample_plans")
    _check_line_limits(r, "sample")
    t, d = _texts(r), _dump(r)
    _assert("【サンプル】" in t and "サンプル佐藤" in d, "sample labelled")
    _assert("confirm_plan" not in _postback_actions(r), "sample has no confirm buttons")
    _assert("古典ソルバ" in t, "honest labelling kept")
    _assert("セットアップ 4/4" in d, f"progress 4/4 after sample")
    s2 = stores.get_store(sid)
    _assert(s2.get("pending_plans") in (None, {}), "sample must not create pending plans")
    _assert(s2.get("confirmed_plan") in (None, {}), "sample must not confirm")

    # 5) 続きから → done
    r = pb(mgr, "v=1&action=resume_setup")
    _assert("セットアップ完了" in _texts(r), "resume → done")

    # 6) staff joins via buttons → name → manager notified
    wh.JOIN_NOTICES.clear()
    st1 = "U_onb_staff1"
    r = pb(st1, "v=1&action=onboard_staff")
    _assert("招待コード" in _texts(r), "ask invite code")
    r = text(st1, "ZZZZZ9")
    _assert("見つかりません" in _texts(r), "unknown code rejected")
    r = text(st1, code.lower())
    _assert("お名前" in _texts(r), f"ask name after code: {_texts(r)}")
    _assert(not wh.JOIN_NOTICES, "no notice before name")
    r = text(st1, "太郎")
    _assert("太郎" in _texts(r), "name set")
    _assert(wh.JOIN_NOTICES and wh.JOIN_NOTICES[-1]["text"] == "太郎さんが参加しました（2/20名）",
            f"join notice: {wh.JOIN_NOTICES}")
    _assert(wh.JOIN_NOTICES[-1]["targets"] == 1, "notice goes to the manager")
    _assert(wh.JOIN_NOTICES[-1]["results"][0]["sent"] is False, "demo mode: not really sent")

    # text registration with name → immediate notice
    r = text("U_onb_staff2", f"登録 {code} 花子")
    _assert(wh.JOIN_NOTICES[-1]["text"] == "花子さんが参加しました（3/20名）", "join notice via text")
    # staff can't open invite/QR
    _assert("店長のみ" in _texts(pb(st1, "v=1&action=show_invite")), "invite manager-only")
    _assert("店長のみ" in _texts(pb(st1, "v=1&action=sample_plans")), "sample manager-only")

    # with >=2 real staff the same button builds real plans (pending stored)
    r = pb(mgr, "v=1&action=sample_plans")
    _assert("【サンプル】" not in _texts(r) and "confirm_plan" in _postback_actions(r), "real plans when staff>=2")
    _assert(stores.get_store(sid).get("pending_plans"), "real plans pending")

    # 7) second store (isolation) + wages on A
    mgr_b = "U_onb_mgr_b"
    pb(mgr_b, "v=1&action=onboard_manager")
    text(mgr_b, "<b>B店</b>&x")
    pb(mgr_b, "v=1&action=ack_terms")
    rb = pb(mgr_b, "v=1&action=agree")
    store_b = stores.get_store_for_user(mgr_b)
    _assert(store_b and store_b["store_id"] != sid, "B created")
    _assert(code not in _dump(rb) and "青山カフェ" not in _dump(rb), "B invite step leaks nothing of A")
    stores.set_member_profile(store_id=sid, display_name="太郎", hourly_wage=1777)

    client = wh.app.test_client()
    resp = client.get(f"/invite/{code}")
    _assert(resp.status_code == 200, "invite page 200")
    page = resp.get_data(as_text=True)
    _assert("青山カフェ" in page and code in page, "page shows store name + code")
    _assert("LINEで友だち追加" in page and "https://line.me/R/ti/p/@813jdxwv" in page, "friend add button")
    for leak in ("太郎", "花子", "1777", "1100", "時給", sid, mgr, st1, store_b["invite_code"], "B店"):
        _assert(leak not in page, f"invite page leaks {leak!r}")
    resp_b = client.get(f"/invite/{store_b['invite_code']}")
    page_b = resp_b.get_data(as_text=True)
    _assert("&lt;b&gt;B店&lt;/b&gt;&amp;x" in page_b and "<b>B店</b>" not in page_b, "store name HTML-escaped")
    _assert("青山カフェ" not in page_b and code not in page_b, "B page leaks nothing of A")
    _assert(client.get("/invite/NOPE99").status_code == 404, "unknown code 404")
    _assert(client.get("/invite/../stores").status_code == 404, "path junk 404")
    q = client.get(f"/invite/{code}/qr.png")
    _assert(q.status_code == 200 and q.mimetype == "image/png" and q.data[:8] == b"\x89PNG\r\n\x1a\n", "qr png")
    _assert(client.get("/invite/NOPE99/qr.png").status_code == 404, "unknown qr 404")

    # 8) referral: share text carries no store data; code stored per store
    r = pb(mgr, "v=1&action=refer_service")
    t = _texts(r)
    ref = stores.get_store(sid).get("referral_code")
    _assert(ref and ref in t, "referral code shown + stored")
    _assert("https://line.me/R/ti/p/@813jdxwv" in t, "referral has OA URL")
    for leak in ("青山カフェ", code, "太郎", "花子"):
        _assert(leak not in _dump(r), f"referral leaks {leak!r}")
    _assert("使えません" in _texts(text(mgr, f"紹介コード {ref}")), "own referral rejected")
    _assert("登録しました" in _texts(text(mgr_b, f"紹介コード {ref}")), "referral applied to B")
    _assert(stores.get_store(store_b["store_id"]).get("referred_by") == ref, "referred_by stored")

    # 9) /demo/message endpoint drives the same flow
    uid = "U_onb_demo"
    j = client.post("/demo/message", json={"userId": uid, "postback": "v=1&action=onboard_manager"}).get_json()
    _assert("お店の名前" in (j["preview_text"] or ""), "demo onboard")
    j = client.post("/demo/message", json={"userId": uid, "text": "デモ食堂"}).get_json()
    _assert(j["store"] and j["store"]["store_name"] == "デモ食堂", "demo store created")

    # 10) legacy text commands still work
    r = text("U_legacy", "店舗作成 旧式店")
    _assert("旧式店" in _texts(r), "店舗作成 text command")

    print("[onb] ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
