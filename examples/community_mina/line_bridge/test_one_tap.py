#!/usr/bin/env python3
"""ワンタップ来月シフト作成."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

JST = ZoneInfo("Asia/Tokyo")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _blob(msgs) -> str:
    return json.dumps(msgs, ensure_ascii=False)


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_ot_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ["LINE_DEMO_MODE"] = "true"
    os.environ["PAYMENT_PROVIDER"] = "mock"
    import importlib
    import one_tap_shift as ots
    import shift_rules as sr
    import stores
    import webhook_app as wh
    import billing

    for m in (ots, sr, stores, wh, billing):
        importlib.reload(m)

    # freeze: Oct 7 2026 → next month = November 2026
    fixed = datetime(2026, 10, 7, 12, 0, tzinfo=JST)
    sr.now_jst = lambda: fixed  # type: ignore

    dates = sr.next_month_dates(today=fixed.date())
    _assert(dates[0] == "2026-11-01" and dates[-1] == "2026-11-30", dates[:3])

    handle = wh.handle_text_message
    ok, msg, store = stores.create_store_as_manager("U_ot_m", "ワンタップ店", display_name="店長")
    _assert(ok, msg)
    sid = store["store_id"]
    handle("U_ot_m", "上記を確認しました")
    handle("U_ot_m", "同意する")
    inv = stores.get_store(sid)["invite_code"]
    handle("U_ot_s1", f"登録 {inv} 太郎")
    handle("U_ot_s2", f"登録 {inv} 花子")

    # staff blocked
    body = "\n".join(m.get("text") or "" for m in handle("U_ot_s1", "来月のシフトを作る"))
    _assert("店長" in body, body[:300])

    # incomplete prefs → choice
    msgs = handle("U_ot_m", "来月のシフトを作る")
    blob = _blob(msgs)
    _assert("未提出" in blob, blob[:600])
    _assert("催促して待つ" in blob or "未提出者に催促" in blob, blob[:800])
    _assert("入れない扱いで作成" in blob, blob[:800])

    # force create treating missing as unavailable
    msgs = wh.handle_postback_message("U_ot_m", "v=1&action=one_tap_force")
    blob = _blob(msgs)
    _assert("来月のシフト案" in blob or "おすすめ" in blob, blob[:800])
    _assert("2026年11月" in blob or "11月" in blob, blob[:900])
    _assert("この案で確定" in blob, blob[:900])
    _assert("3案を見る" in blob, blob[:900])
    _assert("古典" in blob and "量子計算は使っていません" in blob, "honest label")
    _assert("入れない" in blob, "missing treated note")

    store = stores.get_store(sid)
    pending = store.get("pending_rule_plans") or {}
    _assert(pending.get("one_tap"), pending.keys())
    _assert(len(pending.get("dates") or []) == 30, len(pending.get("dates") or []))
    best_key = pending.get("best_key")
    _assert(best_key in {"prefer", "cost", "balance"}, best_key)
    plan = next(p for p in pending["plans"] if p["key"] == best_key)
    _assert("labor_cost" in plan["metrics"], plan["metrics"])

    # show 3
    msgs3 = wh.handle_postback_message("U_ot_m", "v=1&action=one_tap_show3")
    _assert("案1" in _blob(msgs3) or "希望優先" in _blob(msgs3), _blob(msgs3)[:500])

    # confirm → completion card + dated_shifts
    msgs_c = wh.handle_postback_message("U_ot_m", f"v=1&action=confirm_rule_plan&key={best_key}")
    bc = _blob(msgs_c)
    _assert("完成しました" in bc, bc[:900])
    _assert("人件費" in bc and "制約違反" in bc and "計算時間" in bc, bc[:900])
    _assert("スタッフ" in bc and "通知" in bc, bc[:900])
    store = stores.get_store(sid)
    _assert("2026-11-01" in (store.get("dated_shifts") or {}), "nov shifts saved")
    _assert((store.get("confirmed_rule_plan") or {}).get("one_tap"), "flag")

    # isolation
    ok2, _, other = stores.create_store_as_manager("U_ot_x", "秘密ワンタップ店", display_name="他")
    _assert(ok2, "other")
    _assert("秘密ワンタップ店" not in bc, "no leak")

    # FREE → upgrade / week preview
    stores.set_store_subscription(sid, plan="FREE", subscription_status="none", current_period_end=None)
    msgs_f = handle("U_ot_m", "来月のシフトを作る")
    bf = _blob(msgs_f)
    _assert("スタンダード" in bf or "有料" in bf or "フリー" in bf, bf[:600])
    _assert("1週間プレビュー" in bf, bf[:600])
    msgs_p = wh.handle_postback_message("U_ot_m", "v=1&action=one_tap_preview")
    # may hit incomplete prefs again
    bp = _blob(msgs_p)
    _assert("未提出" in bp or "おすすめ" in bp or "プレビュー" in bp or "7" in bp or "案" in bp, bp[:700])
    if "入れない扱いで作成" in bp:
        msgs_p = wh.handle_postback_message("U_ot_m", "v=1&action=one_tap_force")
        bp = _blob(msgs_p)
    store = stores.get_store(sid)
    pending = store.get("pending_rule_plans") or {}
    if pending.get("dates"):
        _assert(len(pending["dates"]) <= 7, f"preview days {len(pending['dates'])}")

    # restore STANDARD and full path with all submitted
    stores.set_store_subscription(sid, plan="STANDARD", subscription_status="trialing")
    nov = sr.next_month_dates(today=fixed.date())

    def mark_all(st):
        done = st.setdefault("availability_done", {}).setdefault(nov[0], {})
        for wid in sr.worker_ids(st):
            done[wid] = fixed.isoformat()
            av = st.setdefault("availability", {}).setdefault(wid, {})
            for d in nov:
                av[d] = {"status": "ok", "start": "10:00", "end": "22:00"}
    stores.update_store(sid, mark_all)
    msgs = handle("U_ot_m", "来月のシフトを作る")
    blob = _blob(msgs)
    _assert("おすすめ" in blob or "来月のシフト案" in blob, blob[:800])
    _assert("未提出" not in blob.split("おすすめ")[0] or "入れない" not in blob[:400], "should skip incomplete gate")
    store = stores.get_store(sid)
    pending = store.get("pending_rule_plans") or {}
    best = next(p for p in pending["plans"] if p["key"] == pending["best_key"])
    sample = {
        "period": pending.get("period_label"),
        "days": len(pending["dates"]),
        "best": best["label"],
        "labor_cost": best["metrics"]["labor_cost"],
        "pref_rate": best["metrics"].get("pref_rate"),
        "violations": best["metrics"]["violation_total"],
        "seconds": best["seconds"],
    }
    print("[ot] ALL ASSERTIONS PASSED")
    print("---SAMPLE---")
    print(json.dumps(sample, ensure_ascii=False, indent=2))
    # completion flex for screenshot
    card = ots.completion_card(
        stores.get_store(sid), pending, best,
        notify_detail="スタッフへの個別通知: 3/3名（デモ）",
        elapsed_ms=best["seconds"] * 1000,
    )
    Path("/tmp/one_tap_flex.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
