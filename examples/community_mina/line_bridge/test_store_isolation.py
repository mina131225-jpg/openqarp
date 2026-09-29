#!/usr/bin/env python3
"""マルチテナント店舗隔離の回帰テスト（PoC）。

2店舗・2店長を作り、クロス店舗の読取／書込が失敗することを断言する。
本番 stores.json は触らない（一時 LINE_STORES_PATH）。
"""

from __future__ import annotations

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
    tmp = tempfile.NamedTemporaryFile(prefix="stores_iso_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    # owner code unset for these tests
    os.environ.pop("LINE_STORE_OWNER_CODE", None)

    # fresh imports after env
    import importlib
    import stores as stores_mod
    import payroll as payroll_mod
    import webhook_app as wh

    importlib.reload(stores_mod)
    importlib.reload(payroll_mod)
    importlib.reload(wh)

    stores = stores_mod
    payroll = payroll_mod
    handle = wh.handle_text_message

    print(f"[iso] temp db: {tmp.name}")

    # --- create two stores / managers ---
    ok_a, msg_a, store_a = stores.create_store_as_manager("U_mgr_A", "A店", display_name="店長A")
    _assert(ok_a and store_a is not None, f"create A failed: {msg_a}")
    ok_b, msg_b, store_b = stores.create_store_as_manager("U_mgr_B", "B店", display_name="店長B")
    _assert(ok_b and store_b is not None, f"create B failed: {msg_b}")
    sid_a, sid_b = store_a["store_id"], store_b["store_id"]
    code_a, code_b = store_a["invite_code"], store_b["invite_code"]
    # 販売版の店長同意（以降の管理操作をテスト可能にする）
    for manager_id in ("U_mgr_A", "U_mgr_B"):
        _assert("確認しました" in "\n".join(m.get("text") or "" for m in handle(manager_id, "上記を確認しました")), "terms acknowledgement")
        _assert("同意を記録" in "\n".join(m.get("text") or "" for m in handle(manager_id, "同意する")), "terms consent")
    _assert(sid_a != sid_b, "store ids must differ")
    _assert(code_a != code_b, "invite codes must differ")
    print(f"[iso] A={sid_a}/{code_a} B={sid_b}/{code_b}")

    # staff on each store
    ok, _, _ = stores.register_user("U_staff_A", code_a, display_name="太郎A")
    _assert(ok, "staff A register")
    ok, _, _ = stores.register_user("U_staff_B", code_b, display_name="花子B")
    _assert(ok, "staff B register")

    # wages only on A
    ok, _, _ = stores.set_member_profile(
        store_id=sid_a, display_name="太郎A", hourly_wage=1500
    )
    _assert(ok, "set wage A")
    ok, _, _ = stores.set_member_profile(
        store_id=sid_b, display_name="花子B", hourly_wage=1300
    )
    _assert(ok, "set wage B")

    # --- cross-store identity ---
    sa = stores.get_store_for_user("U_mgr_A")
    sb = stores.get_store_for_user("U_mgr_B")
    _assert(sa and sa["store_id"] == sid_a, "mgr A store")
    _assert(sb and sb["store_id"] == sid_b, "mgr B store")
    _assert(stores.user_belongs_to_store("U_mgr_A", sid_a), "belongs A")
    _assert(not stores.user_belongs_to_store("U_mgr_A", sid_b), "A must not belong B")

    # find_worker_id is store-scoped: A store must not resolve B staff name
    wid_b_in_a = payroll.find_worker_id(sa, "花子B")
    _assert(wid_b_in_a is None, "B staff name must not resolve in store A")
    wid_a_in_a = payroll.find_worker_id(sa, "太郎A")
    _assert(wid_a_in_a is not None, "A staff must resolve in store A")

    # --- manager A cannot escalate to store B ---
    ok, note, _ = stores.register_manager("U_mgr_A", code_b)
    _assert(not ok, f"mgr A must not become mgr of B: {note}")
    _assert("隔離" in note or "できません" in note, f"unexpected msg: {note}")
    # still on A
    _assert(stores.get_store_for_user("U_mgr_A")["store_id"] == sid_a, "mgr A still on A")

    # --- random outsider cannot steal manager on store with existing manager ---
    ok, note, st = stores.register_manager("U_outsider", code_a, display_name="外部")
    _assert(ok, f"outsider may join as staff: {note}")
    outsider = stores.get_member(st, "U_outsider")
    _assert(outsider is not None and not outsider.get("is_manager"), "outsider must NOT be manager")

    # --- webhook: mgr A payroll must not mention 花子B / 1300 from B ---
    msgs = handle("U_mgr_A", "給与 今月")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("花子B" not in body, f"leak B staff in A payroll:\\n{body[:500]}")
    _assert("1300" not in body, f"leak B wage in A payroll:\\n{body[:500]}")
    _assert("太郎A" in body or "A店" in body, f"expected A content:\\n{body[:500]}")

    # mgr A asking for B staff by name → not found (store-scoped)
    msgs = handle("U_mgr_A", "給与 花子B")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("見つかりません" in body or "給与見込みを作れません" in body, f"expected miss: {body}")

    # staff A cannot see coworker wages via 給与 店長A if different worker — mgr is different
    # set another staff on A
    stores.register_user("U_staff_A2", code_a, display_name="次郎A")
    stores.set_member_profile(store_id=sid_a, display_name="次郎A", hourly_wage=1600)
    msgs = handle("U_staff_A", "給与 次郎A")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("店長のみ" in body, f"staff must not see coworker payroll: {body}")

    # staff A 「給与 今月」→ own only, not 次郎A wage list
    msgs = handle("U_staff_A", "給与 今月")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("次郎A" not in body, f"staff month view must not list coworker: {body[:400]}")

    # --- budget / wage write stays on caller's store ---
    msgs = handle("U_mgr_A", "人件費予算 999999")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("999,999" in body or "999999" in body, f"budget set: {body[:300]}")
    sa2 = stores.get_store(sid_a)
    sb2 = stores.get_store(sid_b)
    _assert(sa2.get("labor_budget_monthly") == 999999, "budget on A")
    _assert(sb2.get("labor_budget_monthly") != 999999, "budget must not hit B")

    # set wage on A staff via manager A — B unchanged
    msgs = handle("U_mgr_A", "時給 太郎A 1700")
    sa3 = stores.get_store(sid_a)
    profiles_a = stores.staff_profiles_for_store(sa3)
    profiles_b = stores.staff_profiles_for_store(stores.get_store(sid_b))
    _assert(any(p.get("hourly_wage") == 1700 for p in profiles_a.values()), "A wage updated")
    _assert(all(p.get("display_name") != "太郎A" for p in profiles_b.values()), "A name absent in B")
    _assert(any(p.get("display_name") == "花子B" and p.get("hourly_wage") == 1300 for p in profiles_b.values()), "B wage intact")

    # create_store via webhook
    msgs = handle("U_new_owner", "店舗作成 テストC店")
    body = "\n".join(m.get("text") or "" for m in msgs)
    _assert("作成しました" in body or "招待コード" in body, f"create via webhook: {body[:400]}")
    sc = stores.get_store_for_user("U_new_owner")
    _assert(sc and sc["store_name"] == "テストC店", "new store attached")
    _assert(stores.is_user_manager(sc, "U_new_owner"), "creator is manager")

    print("[iso] ALL ASSERTIONS PASSED")
    try:
        Path(tmp.name).unlink(missing_ok=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
