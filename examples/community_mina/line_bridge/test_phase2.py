#!/usr/bin/env python3
"""シフト希望・条件付き自動作成・量子比較・勤怠・給与連動の回帰テスト（一時 stores.json、実送信なし）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _texts(msgs) -> str:
    return "\n".join(m.get("text") or "" for m in msgs if m.get("type") == "text")


def _dump(msgs) -> str:
    return json.dumps(msgs, ensure_ascii=False)


def _actions(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("action"), dict):
            yield obj["action"]
        for v in obj.values():
            yield from _actions(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _actions(v)


def _limits(msgs, where):
    _assert(1 <= len(msgs) <= 5, f"{where}: 1..5 messages")
    for m in msgs:
        items = (m.get("quickReply") or {}).get("items") or []
        _assert(len(items) <= 13, f"{where}: quick reply count")
        for it in items:
            _assert(len(it["action"]["label"]) <= 20, f"{where}: qr label {it['action']['label']}")
        if m.get("type") == "text":
            _assert(len(m["text"]) <= 5000, f"{where}: text too long")
    for a in _actions(msgs):
        if a.get("type") in {"postback", "datetimepicker"}:
            _assert(len(a["data"]) <= 300, f"{where}: postback data")


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_p2_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ["LINE_DEMO_MODE"] = "true"
    os.environ["PAYMENT_PROVIDER"] = "mock"
    os.environ["QARP_SKIP_ABI_CHECK"] = "1"
    os.environ.pop("LINE_STORE_OWNER_CODE", None)

    import importlib
    import billing, stores, shift_rules as sr, slot_optimizer as so, attendance as att, quantum_compare as qc, payroll, webhook_app as wh  # noqa: E401
    for mod in (billing, stores, sr, so, att, qc, payroll, wh):
        importlib.reload(mod)

    clock = {"now": datetime(2026, 10, 1, 9, 0, tzinfo=sr.JST)}
    sr.now_jst = lambda: clock["now"]  # 固定時刻（Asia/Tokyo）

    text, pb = wh.handle_text_message, wh.handle_postback_message
    print(f"[p2] temp db: {tmp.name}")

    # --- store A (manager + 4 staff) / store B ---
    mgr = "U_p2_mgr"
    ok, _, store = stores.create_store_as_manager(mgr, "p2店", display_name="店長")
    _assert(ok, "create A")
    text(mgr, "上記を確認しました"); text(mgr, "同意する")
    sid, code = store["store_id"], store["invite_code"]
    staff = {"太郎": "U_taro", "花子": "U_hanako", "次郎": "U_jiro", "美咲": "U_misaki"}
    for name, uid in staff.items():
        text(uid, f"登録 {code} {name}")
    mgr_b = "U_p2_mgr_b"
    ok, _, store_b = stores.create_store_as_manager(mgr_b, "他店", display_name="他店長")
    text(mgr_b, "上記を確認しました"); text(mgr_b, "同意する")
    text("U_other_staff", f"登録 {store_b['invite_code']} 他人")

    dates = sr.planning_dates(stores.get_store(sid))
    _assert(dates[0] == "2026-10-05" and len(dates) == 7, f"period: {dates}")

    # === #2 シフト希望 ===
    r = text(staff["太郎"], "10/5 16:00〜22:00入れます\n10/6 NG\n10/7 入れます")
    _limits(r, "avail text")
    _assert("保存しました" in _texts(r) and "10/6(火): NG" in _texts(r), f"avail saved: {_texts(r)}")
    r = pb(staff["花子"], "v=1&action=avail_menu")
    _limits(r, "avail menu")
    _assert(any(a.get("type") == "datetimepicker" and a.get("mode") == "date" for a in _actions(r)), "datetimepicker button")
    r = pb(staff["花子"], "v=1&action=avail_pick&date=2026-10-05")
    _limits(r, "avail pick")
    _assert("10/5(月)" in _texts(r), "pick shows date")
    r = pb(staff["花子"], "v=1&action=avail_set&date=2026-10-05&st=ok&slot=昼")
    _assert("10:00〜16:00 入れます" in _texts(r), f"slot ok via button: {_texts(r)}")
    r = pb(staff["花子"], "v=1&action=avail_set&date=2026-10-05&st=ok&slot=夜")
    _assert("10:00〜22:00 入れます" in _texts(r), f"merged windows: {_texts(r)}")
    text(staff["次郎"], "10/5 NG")
    pb(staff["次郎"], "v=1&action=avail_done")
    sa = stores.get_store(sid)
    wid = {n: next(m["worker_id"] for m in sa["members"] if m.get("display_name") == n) for n in staff}
    _assert(sa["availability"][wid["太郎"]]["2026-10-06"]["status"] == "ng", "stored per staff")
    _assert(set(sa["availability"]) == {wid["太郎"], wid["花子"], wid["次郎"]}, "only submitters stored")
    # 自分の希望は本人分のみ
    mine = _texts(pb(staff["太郎"], "v=1&action=avail_mine"))
    _assert("16:00〜22:00" in mine and "花子" not in mine, "own availability only")
    # スタッフは集計・条件を見られない／他店の希望は別店舗に保存
    _assert("店長のみ" in _texts(pb(staff["太郎"], "v=1&action=avail_tally")), "tally manager only")
    _assert("店長のみ" in _texts(text(staff["太郎"], "人件費を月30万円以内")), "rules manager only")
    text("U_other_staff", "10/5 NG")
    _assert(not (stores.get_store(sid).get("availability") or {}).get("A_other"), "noop")
    sb = stores.get_store(store_b["store_id"])
    _assert(len(sb["availability"]) == 1 and len(stores.get_store(sid)["availability"]) == 3, "availability isolated per store")
    # 店長の集計
    r = pb(mgr, "v=1&action=avail_tally")
    _limits(r, "tally")
    t = _texts(r)
    _assert("提出: 3/5名" in t, f"tally submitted: {t}")
    _assert("未提出: " in t and "美咲" in t and "店長" in t and "太郎" not in t.split("未提出: ")[1].split("\n")[0], "missing list")
    _assert("10/5(月): 昼 1/2⚠  夜 2/2✅" in t, f"coverage per slot: {t}")
    _assert("他人" not in t, "B staff not in A tally")
    r = pb(mgr_b, "v=1&action=avail_tally")
    _assert("太郎" not in _texts(r) and "提出: 1/2名" in _texts(r), "B tally isolated")

    # === #3 条件 ===
    for cmd, expect in [
        ("営業時間 10:00-22:00", "10:00〜22:00"),
        ("必要人数 夜 2", "夜2人"),
        ("人件費を月30万円以内", "月 300,000円"),
        ("連勤は4日まで", "4 日"),
        ("太郎は新人", "新人"), ("次郎は新人", "新人"), ("美咲は新人", "新人"),
        ("花子はベテラン", "ベテラン"), ("店長はベテラン", "ベテラン"),
        ("新人とベテランを必ず1人ずつ", "オン"),
        ("太郎と花子は同じ時間に入れない", "同じ時間に入れない"),
    ]:
        r = text(mgr, cmd)
        _limits(r, cmd)
        _assert(expect in _texts(r), f"rule {cmd}: {_texts(r)[:200]}")
    rules = sr.get_rules(stores.get_store(sid))
    _assert(rules["require_mix"] and rules["max_consecutive"] == 4 and rules["cost_cap"] == {"period": "month", "yen": 300000}, "rules stored")
    _assert(sorted([wid["太郎"], wid["花子"]]) in [sorted(p) for p in rules["ng_pairs"]], "ng pair stored")
    r = pb(mgr, "v=1&action=rule_req&slot=昼&d=1")
    _assert("昼3人" in _texts(r), "button +1")
    pb(mgr, "v=1&action=rule_req&slot=昼&d=-1")
    _assert(sr.get_rules(stores.get_store(sid)).get("max_consecutive") == 4, "other rules kept")
    _assert(not stores.get_store(store_b["store_id"]).get("shift_rules"), "rules isolated")

    r = pb(mgr, "v=1&action=rule_plans")
    _limits(r, "rule plans")
    t, d = _texts(r), _dump(r)
    _assert("複数条件から最適化エンジン（古典ソルバ）がシフト候補を自動探索" in t, "honest marketing copy")
    for bad in ("AI＋量子", "AI+量子", "量子で作成", "量子で作りました"):
        _assert(bad not in d, f"dishonest phrase {bad}")
    _assert("制約違反" in t and _dump(r[1]["contents"]).count("confirm_rule_plan") == 3 and _dump(r[1]["contents"]).count("action=qcompare&") == 3, "3 plans w/ confirm + qcompare")
    for a in _actions(r):
        if "qcompare" in (a.get("data") or ""):
            _assert("QAOA" not in a.get("label", "") and a["label"].startswith("⚛️"), "main button label has no QAOA")
    bundle = stores.get_store(sid)["pending_rule_plans"]
    problem = so.build_problem(stores.get_store(sid), bundle["dates"])
    for p in bundle["plans"]:
        a = so.assign_from_plan(p)
        _assert(wid["太郎"] not in a[("2026-10-06", "昼")] + a[("2026-10-06", "夜")], "NG day respected")
        _assert(wid["次郎"] not in a[("2026-10-05", "昼")] + a[("2026-10-05", "夜")], "NG day respected (次郎)")
        re_m = so.evaluate(problem, a)
        _assert(re_m["violation_total"] == p["metrics"]["violation_total"], "violation count reproducible")
        pairs = sum(1 for k, ws in a.items() if wid["太郎"] in ws and wid["花子"] in ws)
        _assert(pairs == p["metrics"]["violations"]["ng_pair"], "ng_pair count honest")
        _assert(p["method"] == so.METHOD, "classical method label")

    # === #3b 量子で比べる ===
    r = pb(mgr, "v=1&action=qcompare&key=balance")
    _assert("プロ" in _texts(r), f"qcompare gated to PRO: {_texts(r)[:120]}")
    stores.set_store_subscription(sid, plan="PRO", subscription_status="active",
                                  current_period_end=(datetime.now().astimezone() + timedelta(days=30)).isoformat(timespec="seconds"))
    _assert("店長のみ" in _texts(pb(staff["太郎"], "v=1&action=qcompare&key=balance")), "qcompare manager only")
    r = pb(mgr, "v=1&action=qcompare&key=balance")
    _limits(r, "qcompare")
    d = _dump(r)
    _assert(r[0]["type"] == "flex" and "⚛️ 量子で比べる" in d, "panel flex")
    _assert("QAOA" not in d, "no QAOA word on main panel")
    for k in ("人件費", "希望充足率", "制約違反数", "計算時間", "使用アルゴリズム", "古典: greedy heuristic", "量子方式", "シミュレータ", "全探索"):
        _assert(k in d, f"panel has {k}")
    res = stores.get_store(sid)["last_qcompare"]
    c, q, ex = res["classical"], res["quantum"], res["exact"]
    _assert(q["available"], f"quantum ran: {q.get('error')}")
    _assert(q["qubits"] == len(res["sub"]["candidates"]) <= qc.MAX_QUBITS and q["layers"] == 2, "qubits/layers")
    _assert(c["seconds"] > 0 and q["seconds"] > 0 and ex["seconds"] > 0, "measured times")
    _assert("量子" not in c["algorithm"] and "greedy" in c["algorithm"], "classical never labelled quantum")
    cost_by = {x["name"]: x["cost"] for x in res["sub"]["candidates"]}
    _assert(q["labor_cost"] == sum(cost_by[n] for n in q["members"]), "quantum numbers derived from its own answer")
    _assert(c["labor_cost"] == sum(cost_by[n] for n in c["members"]), "classical numbers derived")
    _assert((ex["violation_total"], ex["energy"]) <= (q["violation_total"], q["energy"]), "exact is a lower bound")
    _assert(res["verdict"] in {"引き分け（同じ品質の解）", "量子方式の勝ち（この小問題では）", "古典の勝ち（この小問題では）"}, "verdict")
    exp = qc.verdict(c, q)
    _assert(res["verdict"] == exp, "verdict consistent with numbers")
    det = _texts(pb(mgr, "v=1&action=qcompare_detail"))
    for k in ("QAOA p=2", "シミュレータ", "実機ではありません", "量子計算に入れた条件", "入れなかった条件", res["sub"]["date_label"]):
        _assert(k in det, f"detail has {k}")
    print(f"[p2] qcompare {res['sub']['date_label']} {res['sub']['slot']} n={q['qubits']}: "
          f"classical {c['labor_cost']}円 v{c['violation_total']} {c['seconds']*1000:.2f}ms / "
          f"quantum {q['labor_cost']}円 v{q['violation_total']} {q['seconds']*1000:.1f}ms / exact {ex['labor_cost']}円 → {res['verdict']}")

    # 遅い場合はバックグラウンド計算 → push（LINE の reply token 期限対策）
    wh.QCOMPARE_SYNC_SECONDS = 0.0
    stores.update_store(sid, lambda s: s.pop("last_qcompare", None))
    r = pb(mgr, "v=1&action=qcompare&key=cost")
    _assert("計算中" in _texts(r), "async reply while computing")
    wh.QCOMPARE_JOBS[sid].join(60)
    _assert(stores.get_store(sid).get("last_qcompare", {}).get("plan_key") == "cost", "async result stored (pushed)")
    wh.QCOMPARE_SYNC_SECONDS = 8.0

    # === confirm → staff sees own dated shift ===
    r = pb(mgr, "v=1&action=confirm_rule_plan&key=balance")
    _assert("確定しました" in _texts(r), "confirm rule plan")
    sa = stores.get_store(sid)
    _assert(set(bundle["dates"]) <= set(sa["dated_shifts"]), "dated shifts saved")
    _assert(not stores.get_store(store_b["store_id"]).get("dated_shifts"), "dated shifts isolated")
    # 太郎 の勤務日
    taro_days = [dd for dd in bundle["dates"] if so.shifts_for_worker(sa, wid["太郎"], dd)]
    _assert(taro_days, "taro has shifts")
    own = _texts(text(staff["太郎"], "自分のシフト"))
    _assert("あなたの確定シフト" in own and "花子" not in own, f"own dated shift only: {own}")

    # === #1 給与 見込み（確定シフト連動）===
    exp_taro = payroll.planned_hours_from_dated_shifts(sa, wid["太郎"], year=2026, month=10)
    r = text(staff["太郎"], "給与 10月")
    t = _texts(r)
    _assert("見込み（確定シフトの日付・時間から自動計算）" in t, f"staff payroll basis: {t[:300]}")
    _assert(f"勤務時間: {exp_taro['hours']:g}h" in t, "hours from dated shifts")
    _assert("花子" not in t, "staff payroll only own")
    t = _texts(text(mgr, "給与 10月"))
    _assert("見込み・確定シフト" in t, "manager payroll uses dated shifts")

    # === #4 勤怠 ===
    d0 = taro_days[0]
    win = so.worker_window(sa, wid["太郎"], d0)
    y, mo, dd = map(int, d0.split("-"))
    st_h, st_m = map(int, win[0].split(":"))
    en_h, en_m = map(int, win[1].split(":"))
    clock["now"] = datetime(y, mo, dd, st_h, st_m, tzinfo=sr.JST) + timedelta(minutes=12)
    r = pb(staff["太郎"], "v=1&action=clock_in")
    _limits(r, "clock in")
    _assert("出勤を記録しました" in _texts(r) and "遅刻 12分" in _texts(r), f"late detect: {_texts(r)}")
    _assert("すでに" in _texts(text(staff["太郎"], "出勤")), "double clock-in blocked")
    clock["now"] = datetime(y, mo, dd, en_h, en_m, tzinfo=sr.JST) - timedelta(minutes=30)
    r = text(staff["太郎"], "退勤")
    _assert("退勤を記録しました" in _texts(r) and "早退 30分" in _texts(r), f"early leave: {_texts(r)}")
    worked = (en_h * 60 + en_m - 30 - (st_h * 60 + st_m + 12)) / 60
    rec = stores.get_store(sid)["attendance"][d0][wid["太郎"]]
    _assert(abs(rec["hours"] - round(worked, 2)) < 1e-6 and rec["in"].endswith("+09:00"), "hours + JST timestamps")
    row = stores.get_store(sid)["actual_hours_by_month"]["2026-10"][wid["太郎"]]
    _assert(row["source"] == "clock" and abs(row["actual_hours"] - round(worked, 2)) < 1e-6, "clock → payroll actual")
    t = _texts(text(staff["太郎"], "給与 10月"))
    _assert("実績ベース（出勤・退勤の打刻 1日分" in t and "遅刻1・早退1" in t, f"payroll 実績 from clock: {t[:400]}")
    _assert("確定シフトからの見込み" in t, "planned still shown for reference")
    # 自動欠勤: 同じ日に他のスタッフでシフトがあり打刻なし → 終了後に欠勤
    others = [w for s in sa["dated_shifts"][d0]["slots"] for w in s["workers"] if w != wid["太郎"]]
    clock["now"] = datetime(y, mo, dd, 23, 59, tzinfo=sr.JST)
    r = pb(mgr, "v=1&action=att_today")
    t = _texts(r)
    _assert("欠勤（自動）" in t and "遅刻12分" in t and "早退30分" in t, f"auto absent + flags: {t}")
    a_rec = stores.get_store(sid)["attendance"][d0]
    _assert(all(a_rec[w].get("absent_by") == "auto" for w in others), "others auto-absent")
    # 店長の欠勤登録／取消・スタッフ不可
    _assert("店長のみ" in _texts(text(staff["花子"], "欠勤 太郎 10/9")), "staff cannot mark absence")
    r = text(mgr, "欠勤 美咲 10/9")
    _assert("欠勤にしました" in _texts(r), "manager marks absence")
    _assert("取り消しました" in _texts(text(mgr, "欠勤取消 美咲 10/9")), "undo absence")
    # 手入力の実績は打刻で上書きしない
    text(mgr, "実績 花子 80時間")
    stores_h = stores.get_store(sid)["actual_hours_by_month"]["2026-10"][wid["花子"]]
    _assert(stores_h.get("source") == "manual" and stores_h["actual_hours"] == 80.0, "manual actual stored")
    # 他店のスタッフの打刻は他店にだけ
    text("U_other_staff", "出勤")
    _assert("U_other_staff" not in json.dumps(stores.get_store(sid).get("attendance")), "attendance isolated")
    _assert(stores.get_store(store_b["store_id"]).get("attendance"), "B attendance stored in B")
    mine = _texts(text(staff["太郎"], "自分の勤怠"))
    _assert("遅刻 1回" in mine and "早退 1回" in mine and "花子" not in mine, "own attendance only")

    print("[p2] ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
