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
        return deepcopy(s) if s else None


def register_user(
    user_id: str,
    invite_code: str,
    *,
    worker_alias: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """スタッフを店舗に紐付け。

    戻り値: (ok, message, store_or_none)
    既に別店舗にいる場合は移籍（PoC は 1 user = 1 store）。
    """
    uid = (user_id or "").strip()
    code = (invite_code or "").strip().upper()
    if not uid:
        return False, "userId がありません。", None
    if not code:
        return False, "店舗コードを指定してください。例: 「登録 MINA01」", None

    with _LOCK:
        db = _load_unlocked()
        sid = db["invite_index"].get(code)
        if not sid or sid not in db["stores"]:
            return False, f"店舗コード「{code}」が見つかりません。店長に確認してください。", None
        store = db["stores"][sid]

        # 旧店舗から外す
        old_sid = db["user_index"].get(uid)
        if old_sid and old_sid != sid and old_sid in db["stores"]:
            old = db["stores"][old_sid]
            old["line_user_ids"] = [x for x in old.get("line_user_ids", []) if x != uid]
            old["members"] = [m for m in old.get("members", []) if m.get("user_id") != uid]
            old["updated_at"] = _now_iso()

        already = uid in store.get("line_user_ids", [])
        if not already:
            store.setdefault("line_user_ids", []).append(uid)
            store.setdefault("members", []).append(
                {
                    "user_id": uid,
                    "registered_at": _now_iso(),
                    "worker_alias": worker_alias,
                }
            )
        else:
            # alias 更新のみ
            for m in store.get("members", []):
                if m.get("user_id") == uid and worker_alias:
                    m["worker_alias"] = worker_alias
        db["user_index"][uid] = sid
        store["updated_at"] = _now_iso()
        _save_unlocked(db)
        name = store["store_name"]
        if already:
            msg = f"「{name}」に登録済みです。"
        else:
            msg = f"「{name}」に登録しました。"
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
        rows.append(
            {
                "user_id_masked": mask_user_id(m.get("user_id", "")),
                "registered_at": str(m.get("registered_at") or ""),
                "worker_alias": str(m.get("worker_alias") or "—"),
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
