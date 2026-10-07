#!/usr/bin/env python3
"""店長ダッシュボード（本日勤怠・欠員・今月・未提出）。"""

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
    tmp = tempfile.NamedTemporaryFile(prefix="stores_dash_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ["LINE_DEMO_MODE"] = "true"
    os.environ["PAYMENT_PROVIDER"] = "mock"
    import importlib
    import attendance as att
    import manager_dashboard as dash
    import shift_rules as sr
    import stores
    import webhook_app as wh
    import billing

    for m in (att, dash, sr, stores, wh, billing):
        importlib.reload(m)

    handle = wh.handle_text_message
    today = date(2026, 10, 7)  # Wed
    fixed_now = datetime(2026, 10, 7, 14, 0, tzinfo=JST)

    # freeze time for reproducible dashboard
    sr.now_jst = lambda: fixed_now  # type: ignore

    ok, msg, store = stores.create_store_as_manager("U_dm", "ダッシュ店", display_name="店長D")
    _assert(ok, msg)
    sid = store["store_id"]
    handle("U_dm", "上記を確認しました")
    handle("U_dm", "同意する")
    inv = stores.get_store(sid)["invite_code"]
    handle("U_ds1", f"登録 {inv} 太郎")
    handle("U_ds2", f"登録 {inv} 花子")
    handle("U_ds3", f"登録 {inv} 次郎")

    # staff cannot open dashboard
    msgs = handle("U_ds1", "ダッシュボード")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("店長のみ" in body, body[:300])

    # seed confirmed shifts for today + attendance
    d = today.isoformat()
    def _seed(st):
        st["dated_shifts"] = {
            d: {"slots": [
                {"name": "昼", "start": "10:00", "end": "16:00", "workers": ["A", "B"]},
                {"name": "夜", "start": "16:00", "end": "22:00", "workers": ["C"]},
            ], "plan_key": "balance"}
        }
        st["shift_rules"] = {**sr.get_rules(st), "slots": [
            {"name": "昼", "start": "10:00", "end": "16:00", "required": 2},
            {"name": "夜", "start": "16:00", "end": "22:00", "required": 2},
        ], "slots_auto": False, "cost_cap": {"period": "month", "yen": 200000}}
        st["labor_budget_monthly"] = 200000
        # clock: A working, B done, C absent; night short by 1
        st["attendance"] = {d: {
            "A": {"in": fixed_now.replace(hour=10).isoformat(timespec="seconds"), "shift_start": "10:00", "shift_end": "16:00"},
            "B": {"in": fixed_now.replace(hour=10).isoformat(timespec="seconds"),
                  "out": fixed_now.replace(hour=16).isoformat(timespec="seconds"),
                  "hours": 6.0, "night_hours": 0, "shift_start": "10:00", "shift_end": "16:00"},
            "C": {"absent": True, "absent_by": "manager"},
        }}
        # prefs: only A submitted for planning week
        dates = sr.planning_dates(st, today=today)
        st["availability_done"] = {dates[0]: {"A": fixed_now.isoformat()}}
        st["availability"] = {"A": {dates[0]: {"status": "ok", "start": "10:00", "end": "22:00"}}}
        return None
    stores.update_store(sid, _seed)
    store = stores.get_store(sid)

    # unit snapshot
    snap = dash.today_snapshot(store, today=today)
    _assert(snap["working"] == ["A"], snap)
    _assert(snap["done"] == ["B"], snap)
    _assert(snap["absent"] == ["C"], snap)
    _assert(snap["total_short"] >= 1, f"night should be short: {snap['slots']}")
    prefs = dash.prefs_snapshot(store, today=today)
    _assert(prefs["missing_n"] >= 2, prefs)
    _assert("B" in prefs["missing"] or "C" in prefs["missing"], prefs)

    sc = wh._scenario_for_user("U_dm")[0]
    data = dash.build_dashboard(store, sc, today=today)
    _assert(data["full"] is True, "trial STANDARD → full")
    _assert(data["month"]["planned_pay"] >= 0, data["month"])
    _assert(data["month"]["budget"] == 200000, data["month"])

    msgs = handle("U_dm", "ダッシュボード")
    blob = _blob(msgs)
    _assert("店長ダッシュボード" in blob, blob[:500])
    _assert("出勤中" in blob and "太郎" in blob, "working name")
    _assert("欠勤" in blob and "次郎" in blob, "absent name")
    _assert("未提出" in blob, blob[:800])
    _assert("予定人件費" in blob or "総労働時間" in blob, "payroll section")
    _assert("未提出者にリマインド" in blob and "今日の勤怠" in blob, "quick actions")
    _assert("条件で自動作成" in blob or "条件でシフト作成" in blob, "auto create btn")
    _assert("給与 今月" in blob, "payroll shortcut")

    # alias
    msgs2 = handle("U_dm", "今月の状況")
    _assert("店長ダッシュボード" in _blob(msgs2), "alias 今月の状況")

    # isolation: other store not mentioned
    ok2, _, other = stores.create_store_as_manager("U_other", "秘密店XYZ", display_name="他")
    _assert(ok2, "other")
    _assert("秘密店XYZ" not in _blob(msgs), "no other-store leak")

    # FREE simplified
    stores.set_store_subscription(sid, plan="FREE", subscription_status="none", current_period_end=None)
    store = stores.get_store(sid)
    data_free = dash.build_dashboard(store, sc, today=today)
    _assert(data_free["full"] is False, "FREE simplified")
    _assert(data_free["month"] is None, "no month money on FREE")
    msgs_f = handle("U_dm", "ダッシュボード")
    bf = _blob(msgs_f)
    _assert("店長ダッシュボード" in bf, bf[:400])
    _assert("出勤中" in bf, "FREE still sees today")
    _assert("総労働時間（見込み）" not in bf and "実績人件費" not in bf, "FREE hides pay rows")
    _assert("フリー" in bf or "スタンダード" in bf or "料金プラン" in bf, "upgrade hint")
    _assert("秘密店XYZ" not in bf, "isolation FREE")

    # consent gate
    def clear_consent(st):
        for m in st.get("members") or []:
            if m.get("user_id") == "U_dm":
                m.pop("consent_at", None)
                m.pop("terms_acknowledged_at", None)
    stores.update_store(sid, clear_consent)
    msgs_c = handle("U_dm", "ダッシュボード")
    bc = "\n".join(m.get("text") or "" for m in msgs_c)
    _assert("同意" in bc or "確認" in bc or "注意" in bc, bc[:400])

    print("[dash] ALL ASSERTIONS PASSED")
    # sample numbers for report
    stores.set_store_subscription(sid, plan="STANDARD", subscription_status="trialing")
    store = stores.get_store(sid)
    data = dash.build_dashboard(store, sc, today=today)
    print("---SAMPLE---")
    print(json.dumps({
        "today": {k: data["today"][k] for k in ("label", "working", "done", "absent", "not_in", "total_short", "total_required")},
        "slots": data["today"]["slots"],
        "month": {k: data["month"][k] for k in ("label", "planned_hours", "actual_hours", "planned_pay", "actual_pay", "budget", "budget_diff") } if data["month"] else None,
        "prefs": {k: data["prefs"][k] for k in ("period_label", "submitted_n", "missing_n", "missing")},
    }, ensure_ascii=False, indent=2))
    # write flex for screenshot
    flex = dash.build_dashboard_flex(data, store)
    Path("/tmp/dashboard_flex.json").write_text(json.dumps(flex, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
