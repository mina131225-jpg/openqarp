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
            "members": [],  # {user_id, registered_at, worker_alias?}
            "preferences": prefs,
            "scenario_override": None,
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
    """既存メンバーに worker_id が無い場合、登録順で A/B/C/D を埋める。"""
    changed = False
    members = store.setdefault("members", [])
    used = {
        str(m.get("worker_id") or "").strip()
        for m in members
        if m.get("worker_id")
    }
    for m in members:
        wid = str(m.get("worker_id") or "").strip()
        if wid:
            continue
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
            store.setdefault("members", []).append(
                {
                    "user_id": uid,
                    "registered_at": _now_iso(),
                    "worker_id": slot,
                    "display_name": dn,
                    "worker_alias": dn,  # 互換: alias = 表示名
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


def member_rows_masked(store: dict[str, Any]) -> list[dict[str, str]]:
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


def ensure_demo_store(
    store_name: str = "デモ店舗",
    invite_code: str = "DEMO01",
) -> dict[str, Any]:
    """デモ用店舗が無ければ作る。あればそのまま返す。"""
    existing = get_store_by_invite(invite_code)
    if existing:
        return existing
    return create_store(store_name, invite_code=invite_code)
