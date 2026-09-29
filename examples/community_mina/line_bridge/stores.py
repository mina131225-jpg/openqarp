#!/usr/bin/env python3
"""マルチテナント店舗レジストリ（PoC）。

販売先は「開発者個人 LINE」ではなく、店舗オーナー／スタッフの公式アカウント運用を想定。
店舗ごとに invite_code を発行し、スタッフが「登録 <店舗コード>」で userId を紐付ける。

永続化は JSON（既定 stores.json）。SQLite も同じ API で使えるが、依存を増やさないため
既定は JSON。環境変数 LINE_STORES_PATH でパス変更可。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_DEFAULT_PATH = _HERE / "stores.json"
_LOCK = threading.RLock()

# 招待コード: 読みやすい英数字（混同しやすい文字を除外）
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# シナリオ workers と対応する枠（登録順に割当）
WORKER_SLOTS = ["A", "B", "C", "D"]

# PoC 既定: 時給・1シフト時間（予定人件費／給与見込み用。振込はしない）
DEFAULT_HOURLY_WAGE = 1100
DEFAULT_HOURS_PER_SHIFT = 8.0
DEFAULT_WAGE_PREMIUMS = {
    "weekend": 1.25,  # 土日割増倍率
    "holiday": 1.35,  # 祝日割増（PoC: holiday_days 指定時）
    "night": 1.25,    # 深夜割増（深夜時給未設定時の倍率）
}
DEFAULT_WEEKEND_DAYS = ["土", "日"]
DEFAULT_COMMUTE_ALLOWANCE = 0
DEFAULT_OT_MULTIPLIER = 1.25
MANAGER_OWNER_CODE_ENV = "LINE_STORE_OWNER_CODE"  # optional global owner code


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def stores_path() -> Path:
    env = os.environ.get("LINE_STORES_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else (_HERE / p).resolve()
    return _DEFAULT_PATH


def _empty_db() -> dict[str, Any]:
    return {
        "version": 1,
        "stores": {},
        "invite_index": {},
        "user_index": {},
    }


def _load_unlocked(path: Path | None = None) -> dict[str, Any]:
    p = path or stores_path()
    if not p.exists():
        return _empty_db()
    try:
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return _empty_db()
    if not isinstance(raw, dict):
        return _empty_db()
    raw.setdefault("version", 1)
    raw.setdefault("stores", {})
    raw.setdefault("invite_index", {})
    raw.setdefault("user_index", {})
    return raw


def _save_unlocked(db: dict[str, Any], path: Path | None = None) -> Path:
    p = path or stores_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(db, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)
    return p


def load_db() -> dict[str, Any]:
    with _LOCK:
        return deepcopy(_load_unlocked())


def generate_invite_code(length: int = 6) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def generate_store_id() -> str:
    return "store_" + secrets.token_hex(4)


def invite_url_placeholder(invite_code: str, *, base: str | None = None) -> str:
    """QR／配布用のプレースホルダ URL（実 LINE 友だち追加リンクではない）。

    本番では LINE 公式の友だち追加 URL + 店舗コード案内に差し替える。
    """
    root = (base or os.environ.get("LINE_INVITE_BASE_URL", "")).strip()
    if not root:
        root = "https://line.me/R/ti/p/@YOUR_OFFICIAL_ACCOUNT"
    sep = "&" if "?" in root else "?"
    return f"{root}{sep}invite={invite_code}"


def mask_user_id(user_id: str) -> str:
    """表示用にマスク（先頭2＋末尾4以外を *）。"""
    uid = (user_id or "").strip()
    if len(uid) <= 6:
        return (uid[:1] + "***") if uid else "(empty)"
    return uid[:2] + ("*" * max(3, len(uid) - 6)) + uid[-4:]


def create_store(
    store_name: str,
    *,
    invite_code: str | None = None,
    preferences: dict[str, Any] | None = None,
    scenario_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """店舗を作成して永続化。戻り値は店舗レコード。"""
    name = (store_name or "").strip() or "無題の店舗"
    with _LOCK:
        db = _load_unlocked()
        code = (invite_code or "").strip().upper() or generate_invite_code()
        # 衝突回避
        while code in db["invite_index"]:
            code = generate_invite_code()
        sid = generate_store_id()
        while sid in db["stores"]:
            sid = generate_store_id()
        prefs = deepcopy(preferences) if preferences else {}
        if scenario_seed and "preferred_offs" in scenario_seed and "preferred_offs" not in prefs:
            prefs["preferred_offs"] = deepcopy(scenario_seed["preferred_offs"])
        record = {
            "store_id": sid,
            "store_name": name,
            "invite_code": code,
            "line_user_ids": [],
            "members": [],  # display_name/hourly_wage/available_days/max_hours_week/role/skills/is_manager
            "preferences": prefs,
            "scenario_override": None,
            "hours_per_shift": DEFAULT_HOURS_PER_SHIFT,
            "wage_premiums": dict(DEFAULT_WAGE_PREMIUMS),
            "weekend_days": list(DEFAULT_WEEKEND_DAYS),
            "holiday_days": [],
            "pending_plans": None,
            "confirmed_plan": None,
            "labor_budget_monthly": None,
            "payroll_months": {},
            "actual_hours_by_month": {},
            "night_hours_per_weekend_shift": 2.0,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        db["stores"][sid] = record
        db["invite_index"][code] = sid
        _save_unlocked(db)
        return deepcopy(record)


def list_stores() -> list[dict[str, Any]]:
    with _LOCK:
        db = _load_unlocked()
        return [deepcopy(s) for s in db["stores"].values()]


def get_store(store_id: str) -> dict[str, Any] | None:
    with _LOCK:
        db = _load_unlocked()
        s = db["stores"].get(store_id)
        return deepcopy(s) if s else None


def get_store_by_invite(invite_code: str) -> dict[str, Any] | None:
    code = (invite_code or "").strip().upper()
    if not code:
        return None
    with _LOCK:
        db = _load_unlocked()
        sid = db["invite_index"].get(code)
        if not sid:
            return None
        s = db["stores"].get(sid)
        return deepcopy(s) if s else None


def get_store_for_user(user_id: str) -> dict[str, Any] | None:
    uid = (user_id or "").strip()
    if not uid:
        return None
    with _LOCK:
        db = _load_unlocked()
        sid = db["user_index"].get(uid)
        if not sid:
            return None
        s = db["stores"].get(sid)
        if not s:
            return None
        if _backfill_worker_ids(s):
            s["updated_at"] = _now_iso()
            _save_unlocked(db)
        return deepcopy(s)


def _normalize_display_name(name: str | None) -> str | None:
    """表示名を正規化。空なら None。"""
    if name is None:
        return None
    n = str(name).strip()
    if not n:
        return None
    # Flex 行ラベル向けに短く（PoC）
    if len(n) > 12:
        n = n[:12]
    return n


def _assign_worker_id(store: dict[str, Any]) -> str | None:
    """未使用の A/B/C/D 枠を登録順で返す。空きがなければ None。"""
    used = {
        str(m.get("worker_id") or "").strip()
        for m in (store.get("members") or [])
        if m.get("worker_id")
    }
    for slot in WORKER_SLOTS:
        if slot not in used:
            return slot
    return None


def _backfill_worker_ids(store: dict[str, Any]) -> bool:
    """既存メンバーに worker_id / 時給 / 店長フラグが無い場合を埋める。"""
    changed = False
    members = store.setdefault("members", [])
    used = {
        str(m.get("worker_id") or "").strip()
        for m in members
        if m.get("worker_id")
    }
    for m in members:
        wid = str(m.get("worker_id") or "").strip()
        if not wid:
            for slot in WORKER_SLOTS:
                if slot not in used:
                    m["worker_id"] = slot
                    used.add(slot)
                    changed = True
                    break
        # display_name が無く worker_alias があれば流用
        if not m.get("display_name") and m.get("worker_alias"):
            m["display_name"] = m.get("worker_alias")
            changed = True
        if "hourly_wage" not in m or m.get("hourly_wage") is None:
            m["hourly_wage"] = DEFAULT_HOURLY_WAGE
            changed = True
        if "max_hours_week" not in m:
            m["max_hours_week"] = None
            changed = True
        if "is_manager" not in m:
            m["is_manager"] = False
            changed = True
        if "available_days" not in m:
            m["available_days"] = None
            changed = True
        if "role" not in m:
            m["role"] = None
            changed = True
        if "skills" not in m:
            m["skills"] = []
            changed = True
    # 誰も店長でなければ最初のメンバーを店長に
    if members and not any(bool(m.get("is_manager")) for m in members):
        members[0]["is_manager"] = True
        changed = True
    if store.get("hours_per_shift") is None:
        store["hours_per_shift"] = DEFAULT_HOURS_PER_SHIFT
        changed = True
    if not isinstance(store.get("wage_premiums"), dict):
        store["wage_premiums"] = dict(DEFAULT_WAGE_PREMIUMS)
        changed = True
    else:
        for k, v in DEFAULT_WAGE_PREMIUMS.items():
            if k not in store["wage_premiums"]:
                store["wage_premiums"][k] = v
                changed = True
    if not isinstance(store.get("weekend_days"), list):
        store["weekend_days"] = list(DEFAULT_WEEKEND_DAYS)
        changed = True
    if "holiday_days" not in store:
        store["holiday_days"] = []
        changed = True
    if "pending_plans" not in store:
        store["pending_plans"] = None
        changed = True
    if "confirmed_plan" not in store:
        store["confirmed_plan"] = None
        changed = True
    for m in members:
        if "commute_allowance" not in m:
            m["commute_allowance"] = DEFAULT_COMMUTE_ALLOWANCE
            changed = True
        if "allowances" not in m:
            m["allowances"] = []
            changed = True
        if "night_hourly_wage" not in m:
            m["night_hourly_wage"] = None
            changed = True
        if "overtime_hourly_wage" not in m:
            m["overtime_hourly_wage"] = None
            changed = True
    if "labor_budget_monthly" not in store:
        store["labor_budget_monthly"] = None
        changed = True
    if "payroll_months" not in store:
        store["payroll_months"] = {}
        changed = True
    if "actual_hours_by_month" not in store:
        store["actual_hours_by_month"] = {}
        changed = True
    if "night_hours_per_weekend_shift" not in store:
        store["night_hours_per_weekend_shift"] = 2.0
        changed = True
    return changed


def get_member(store: dict[str, Any] | None, user_id: str) -> dict[str, Any] | None:
    """店舗レコードから該当メンバー（コピー）を返す。"""
    if not store:
        return None
    uid = (user_id or "").strip()
    for m in store.get("members") or []:
        if m.get("user_id") == uid:
            return deepcopy(m)
    return None


def display_labels_for_store(store: dict[str, Any] | None) -> dict[str, str]:
    """worker_id → 表示ラベル（display_name があればそれ、無ければ A/B/C）。"""
    if not store:
        return {}
    labels: dict[str, str] = {}
    for m in store.get("members") or []:
        wid = str(m.get("worker_id") or "").strip()
        if not wid:
            continue
        dn = _normalize_display_name(m.get("display_name") or m.get("worker_alias"))
        labels[wid] = dn if dn else wid
    return labels


def register_user(
    user_id: str,
    invite_code: str,
    *,
    worker_alias: str | None = None,
    display_name: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """スタッフを店舗に紐付け。

    戻り値: (ok, message, store_or_none)
    既に別店舗にいる場合は移籍（PoC は 1 user = 1 store）。
    登録順に worker_id（A/B/C/D）を割当。display_name / worker_alias で表示名を保存。
    """
    uid = (user_id or "").strip()
    code = (invite_code or "").strip().upper()
    if not uid:
        return False, "userId がありません。", None
    if not code:
        return False, "店舗コードを指定してください。例: 「登録 MINA01」", None

    # display_name 優先、なければ worker_alias
    dn = _normalize_display_name(display_name if display_name is not None else worker_alias)

    with _LOCK:
        db = _load_unlocked()
        sid = db["invite_index"].get(code)
        if not sid or sid not in db["stores"]:
            return False, f"店舗コード「{code}」が見つかりません。店長に確認してください。", None
        store = db["stores"][sid]
        _backfill_worker_ids(store)

        # 旧店舗から外す
        old_sid = db["user_index"].get(uid)
        if old_sid and old_sid != sid and old_sid in db["stores"]:
            old = db["stores"][old_sid]
            old["line_user_ids"] = [x for x in old.get("line_user_ids", []) if x != uid]
            old["members"] = [m for m in old.get("members", []) if m.get("user_id") != uid]
            old["updated_at"] = _now_iso()

        already = uid in store.get("line_user_ids", [])
        if not already:
            slot = _assign_worker_id(store)
            store.setdefault("line_user_ids", []).append(uid)
            # 最初の登録者を店長に（既存店長がいなければ）
            has_manager = any(bool(m.get("is_manager")) for m in store.get("members") or [])
            store.setdefault("members", []).append(
                {
                    "user_id": uid,
                    "registered_at": _now_iso(),
                    "worker_id": slot,
                    "display_name": dn,
                    "worker_alias": dn,  # 互換: alias = 表示名
                    "hourly_wage": DEFAULT_HOURLY_WAGE,
                    "night_hourly_wage": None,
                    "overtime_hourly_wage": None,
                    "commute_allowance": DEFAULT_COMMUTE_ALLOWANCE,
                    "allowances": [],
                    "max_hours_week": None,
                    "available_days": None,  # None = 全日可
                    "role": None,
                    "skills": [],
                    "is_manager": not has_manager,
                }
            )
        else:
            for m in store.get("members", []):
                if m.get("user_id") == uid:
                    if not m.get("worker_id"):
                        m["worker_id"] = _assign_worker_id(store)
                    if dn:
                        m["display_name"] = dn
                        m["worker_alias"] = dn
                    break
        db["user_index"][uid] = sid
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        name = store["store_name"]
        member = next((m for m in store.get("members", []) if m.get("user_id") == uid), {})
        slot = member.get("worker_id") or "—"
        shown = member.get("display_name") or ""
        if already:
            msg = f"「{name}」に登録済みです。"
        else:
            msg = f"「{name}」に登録しました。（枠 {slot}）"
        if member.get("is_manager"):
            msg += "\n役割: 店長"
        if shown:
            msg += f"\n表示名: {shown}"
        elif not already:
            msg += "\nまだ表示名がありません。「名前 太郎」で設定できます。"
        return True, msg, deepcopy(store)


def set_member_display_name(
    user_id: str,
    display_name: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    """登録済みスタッフの表示名を更新。"""
    uid = (user_id or "").strip()
    dn = _normalize_display_name(display_name)
    if not uid:
        return False, "userId がありません。", None
    if not dn:
        return False, "表示名を指定してください。例: 「名前 太郎」", None

    with _LOCK:
        db = _load_unlocked()
        sid = db["user_index"].get(uid)
        if not sid or sid not in db["stores"]:
            return False, "まだ店舗に登録されていません。先に「登録 店舗コード」してください。", None
        store = db["stores"][sid]
        _backfill_worker_ids(store)
        found = None
        for m in store.get("members") or []:
            if m.get("user_id") == uid:
                m["display_name"] = dn
                m["worker_alias"] = dn
                if not m.get("worker_id"):
                    m["worker_id"] = _assign_worker_id(store)
                found = m
                break
        if found is None:
            # line_user_ids だけある場合の救済
            slot = _assign_worker_id(store)
            found = {
                "user_id": uid,
                "registered_at": _now_iso(),
                "worker_id": slot,
                "display_name": dn,
                "worker_alias": dn,
            }
            store.setdefault("members", []).append(found)
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        slot = found.get("worker_id") or "—"
        msg = f"表示名を「{dn}」にしました。（枠 {slot}）"
        return True, msg, deepcopy(store)


def update_store_preferences(
    store_id: str,
    preferences: dict[str, Any],
    *,
    merge: bool = True,
) -> dict[str, Any] | None:
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return None
        if merge:
            prefs = dict(store.get("preferences") or {})
            prefs.update(deepcopy(preferences))
            store["preferences"] = prefs
        else:
            store["preferences"] = deepcopy(preferences)
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        return deepcopy(store)


def set_store_preferred_offs(
    store_id: str,
    preferred_offs: dict[str, list[str]],
) -> dict[str, Any] | None:
    return update_store_preferences(
        store_id,
        {"preferred_offs": deepcopy(preferred_offs)},
        merge=True,
    )


def member_rows_masked(store: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for m in store.get("members") or []:
        dn = m.get("display_name") or m.get("worker_alias") or "—"
        rows.append(
            {
                "user_id_masked": mask_user_id(m.get("user_id", "")),
                "registered_at": str(m.get("registered_at") or ""),
                "worker_id": str(m.get("worker_id") or "—"),
                "display_name": str(dn),
                "worker_alias": str(m.get("worker_alias") or dn or "—"),
                "hourly_wage": int(m.get("hourly_wage") or DEFAULT_HOURLY_WAGE),
                "commute_allowance": int(m.get("commute_allowance") or DEFAULT_COMMUTE_ALLOWANCE),
                "max_hours_week": m.get("max_hours_week"),
                "is_manager": bool(m.get("is_manager")),
            }
        )
    # line_user_ids にあって members に無い分も
    known = {m.get("user_id") for m in (store.get("members") or [])}
    for uid in store.get("line_user_ids") or []:
        if uid not in known:
            rows.append(
                {
                    "user_id_masked": mask_user_id(uid),
                    "registered_at": "",
                    "worker_id": "—",
                    "display_name": "—",
                    "worker_alias": "—",
                }
            )
    return rows


def is_user_manager(store: dict[str, Any] | None, user_id: str) -> bool:
    m = get_member(store, user_id)
    return bool(m and m.get("is_manager"))


def staff_profiles_for_store(store: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """worker_id → {display_name, hourly_wage, max_hours_week, is_manager, user_id}。"""
    if not store:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for m in store.get("members") or []:
        wid = str(m.get("worker_id") or "").strip()
        if not wid:
            continue
        dn = _normalize_display_name(m.get("display_name") or m.get("worker_alias"))
        out[wid] = {
            "user_id": m.get("user_id"),
            "display_name": dn or wid,
            "hourly_wage": int(m.get("hourly_wage") or DEFAULT_HOURLY_WAGE),
            "night_hourly_wage": m.get("night_hourly_wage"),
            "overtime_hourly_wage": m.get("overtime_hourly_wage"),
            "commute_allowance": int(m.get("commute_allowance") or DEFAULT_COMMUTE_ALLOWANCE),
            "allowances": list(m.get("allowances") or []),
            "max_hours_week": m.get("max_hours_week"),
            "available_days": m.get("available_days"),
            "role": m.get("role"),
            "skills": list(m.get("skills") or []),
            "is_manager": bool(m.get("is_manager")),
        }
    return out


def hours_per_shift_for_store(store: dict[str, Any] | None) -> float:
    if not store:
        return DEFAULT_HOURS_PER_SHIFT
    try:
        return float(store.get("hours_per_shift") or DEFAULT_HOURS_PER_SHIFT)
    except (TypeError, ValueError):
        return DEFAULT_HOURS_PER_SHIFT


def _find_member_unlocked(store: dict[str, Any], *, user_id: str | None = None, worker_id: str | None = None, display_name: str | None = None) -> dict[str, Any] | None:
    uid = (user_id or "").strip() or None
    wid = (worker_id or "").strip() or None
    dn = _normalize_display_name(display_name)
    for m in store.get("members") or []:
        if uid and m.get("user_id") == uid:
            return m
        if wid and str(m.get("worker_id") or "") == wid:
            return m
        if dn:
            mdn = _normalize_display_name(m.get("display_name") or m.get("worker_alias"))
            if mdn == dn:
                return m
    return None


def set_member_profile(
    user_id: str | None = None,
    *,
    store_id: str | None = None,
    worker_id: str | None = None,
    display_name: str | None = None,
    hourly_wage: int | None = None,
    max_hours_week: float | None = None,
    clear_max_hours: bool = False,
    available_days: list[str] | None = None,
    clear_available_days: bool = False,
    role: str | None = None,
    skills: list[str] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """時給・週上限・勤務可能日・役割を更新（予定人件費シミュレーション用）。

    user_id 指定時はその本人。店長が他人を更新する場合は store_id + worker_id/display_name。
    """
    uid = (user_id or "").strip() or None
    with _LOCK:
        db = _load_unlocked()
        sid = store_id
        if not sid and uid:
            sid = db["user_index"].get(uid)
        if not sid or sid not in db["stores"]:
            return False, "まだ店舗に登録されていません。先に「登録 店舗コード」してください。", None
        store = db["stores"][sid]
        _backfill_worker_ids(store)
        found = _find_member_unlocked(
            store, user_id=uid if not worker_id and not display_name else None,
            worker_id=worker_id, display_name=display_name,
        )
        # 本人指定で worker/display 無し
        if found is None and uid and not worker_id and not display_name:
            found = _find_member_unlocked(store, user_id=uid)
        if found is None:
            return False, "対象スタッフが見つかりません。", None
        if hourly_wage is not None:
            if hourly_wage < 0 or hourly_wage > 100_000:
                return False, "時給は 0〜100000 円の範囲で指定してください。", None
            found["hourly_wage"] = int(hourly_wage)
        if clear_max_hours:
            found["max_hours_week"] = None
        elif max_hours_week is not None:
            if max_hours_week < 0 or max_hours_week > 168:
                return False, "週上限時間は 0〜168 の範囲で指定してください。", None
            found["max_hours_week"] = float(max_hours_week)
        if clear_available_days:
            found["available_days"] = None
        elif available_days is not None:
            found["available_days"] = list(available_days)
        if role is not None:
            found["role"] = (role.strip() or None)
        if skills is not None:
            found["skills"] = [s for s in skills if s]
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        label = found.get("display_name") or found.get("worker_id") or "?"
        wage = int(found.get("hourly_wage") or DEFAULT_HOURLY_WAGE)
        cap = found.get("max_hours_week")
        cap_s = f"{cap:g}時間" if cap is not None else "なし"
        avail = found.get("available_days")
        avail_s = "全日" if not avail else ",".join(avail)
        role_s = found.get("role") or "—"
        msg = (
            f"{label} のプロフィールを更新しました。\n"
            f"時給 {wage}円／週上限 {cap_s}／勤務可能 {avail_s}／役割 {role_s}"
        )
        return True, msg, deepcopy(store)


def set_store_wage_premiums(
    store_id: str,
    premiums: dict[str, float],
    *,
    weekend_days: list[str] | None = None,
    holiday_days: list[str] | None = None,
) -> dict[str, Any] | None:
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return None
        cur = dict(store.get("wage_premiums") or DEFAULT_WAGE_PREMIUMS)
        for k, v in premiums.items():
            if k in ("weekend", "holiday", "night") and isinstance(v, (int, float)):
                cur[k] = float(v)
        store["wage_premiums"] = cur
        if weekend_days is not None:
            store["weekend_days"] = list(weekend_days)
        if holiday_days is not None:
            store["holiday_days"] = list(holiday_days)
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        return deepcopy(store)


def register_manager(
    user_id: str,
    invite_code: str,
    *,
    display_name: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """店長登録。登録＋ is_manager=True。例: 「店長登録 DEMO01」。"""
    uid = (user_id or "").strip()
    code = (invite_code or "").strip().upper()
    if not uid:
        return False, "userId がありません。", None
    if not code:
        return False, "店舗コードを指定してください。例: 「店長登録 DEMO01」", None

    # まず通常登録（既登録でも OK）
    ok, note, store = register_user(uid, code, display_name=display_name)
    if not ok or store is None:
        return ok, note, store

    with _LOCK:
        db = _load_unlocked()
        sid = db["user_index"].get(uid)
        if not sid or sid not in db["stores"]:
            return False, "店舗への紐付けに失敗しました。", None
        store = db["stores"][sid]
        _backfill_worker_ids(store)
        for m in store.get("members") or []:
            if m.get("user_id") == uid:
                m["is_manager"] = True
                break
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        name = store["store_name"]
        msg = f"「{name}」の店長として登録しました。\n{note}"
        return True, msg, deepcopy(store)


def set_pending_plans(store_id: str, pending: dict[str, Any] | None) -> dict[str, Any] | None:
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return None
        store["pending_plans"] = deepcopy(pending) if pending else None
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        return deepcopy(store)


def set_confirmed_plan(store_id: str, plan: dict[str, Any] | None) -> dict[str, Any] | None:
    with _LOCK:
        db = _load_unlocked()
        store = db["stores"].get(store_id)
        if not store:
            return None
        store["confirmed_plan"] = deepcopy(plan) if plan else None
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        return deepcopy(store)


def ensure_demo_store(
    store_name: str = "デモ店舗",
    invite_code: str = "DEMO01",
) -> dict[str, Any]:
    """デモ用店舗が無ければ作る。あればそのまま返す。"""
    existing = get_store_by_invite(invite_code)
    if existing:
        return existing
    return create_store(store_name, invite_code=invite_code)
