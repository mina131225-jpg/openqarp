#!/usr/bin/env python3
"""Unknown / mistyped command handling: role-aware help + near-miss suggestions.

Never includes other-store data or secrets. Japanese copy only.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

Role = Literal["guest", "staff", "manager"]

# (canonical phrase to suggest / type, aliases for fuzzy match)
# Keep phrases short so edit-distance near-misses work well.
_COMMON: list[tuple[str, tuple[str, ...]]] = [
    ("メニュー", ("メニュー", "めにゅー", "メニュ", "menu")),
    ("使い方", ("使い方", "使いがた", "使いかた", "ヘルプ", "ヘルフ", "help", "？", "?")),
    ("プラン", ("プラン", "料金プラン", "料金")),
]

_GUEST: list[tuple[str, tuple[str, ...]]] = [
    ("登録 DEMO01", ("登録", "登緑", "とうろく", "バインド")),
    ("店舗作成 青山店", ("店舗作成", "店舗を作成", "新規店舗")),
    ("店長登録 DEMO01", ("店長登録", "マネージャー登録")),
    ("お店を始める（店長）", ("お店を始める", "店長として始める")),
    ("スタッフとして参加", ("スタッフとして参加", "スタッフ参加")),
]

_STAFF: list[tuple[str, tuple[str, ...]]] = [
    ("出勤", ("出勤", "出金", "しゅっきん", "クロックイン")),
    ("退勤", ("退勤", "退金", "たいきん", "クロックアウト")),
    ("シフト希望を出す", ("シフト希望", "シフト希望を出す", "希望を出す")),
    ("自分のシフト", ("自分のシフト", "マイシフト", "私のシフト")),
    ("シフト見せて", ("シフト見せて", "シフト表", "組表", "スケジュール")),
    ("希望休 日曜", ("希望休", "休み希望", "休希望")),
    ("名前 太郎", ("名前", "表示名", "ニックネーム")),
    ("自分の勤怠", ("自分の勤怠", "勤怠")),
    ("給与 今月", ("給与", "給与見込み")),
]

_MANAGER: list[tuple[str, tuple[str, ...]]] = [
    ("店舗設定", ("店舗設定",)),
    ("スタッフ管理", ("スタッフ管理",)),
    ("シフト作成", ("シフト作成", "シフトをつくる", "シフトを作る", "シフト3案作って")),
    ("人件費・給与", ("人件費・給与", "人件費給与", "給与メニュー")),
    ("今月の状況", ("今月の状況", "ダッシュボード")),
    ("料金プラン", ("料金プラン", "プラン", "料金")),
    ("条件設定", ("条件設定", "条件", "シフト条件")),
    ("提出状況", ("提出状況", "希望の集計")),
    ("条件でシフト作成", ("条件でシフト作成", "条件で自動作成", "自動作成")),
    ("今日の勤怠", ("今日の勤怠", "勤怠")),
    ("スタッフを招待", ("スタッフを招待", "スタッフ招待", "招待")),
    ("申し込む プロ", ("申し込む プロ", "申し込むプロ", "申込む プロ")),
    ("申し込む スタンダード", ("申し込む スタンダード", "申し込むスタンダード")),
]

# Common single-character / glyph typos applied before distance check.
_TYPO_SUBS: tuple[tuple[str, str], ...] = (
    ("登緑", "登録"),
    ("登禄", "登録"),
    ("シスト", "シフト"),
    ("シフト", "シフト"),
    ("ヘルフ", "ヘルプ"),
    ("出金", "出勤"),
    ("退金", "退勤"),
    ("使いがた", "使い方"),
    ("使いかた", "使い方"),
    ("めにゅー", "メニュー"),
    ("メニュ", "メニュー"),
)


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def normalize_for_match(text: str) -> str:
    """Collapse spaces / fullwidth, strip trailing soft particles, apply typo subs."""
    t = _nfkc(text).strip().replace("　", " ")
    t = re.sub(r"[!！?？。．、,，]+$", "", t)
    # trailing soft endings that don't change the command: よ/ね/な/なぁ/よね
    t = re.sub(r"(?:よね|なぁ|なー|よ|ね|な|です|して|ください)$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    low = t.lower()
    for bad, good in _TYPO_SUBS:
        if bad.lower() in low or bad in t:
            t = t.replace(bad, good)
            low = t.lower()
    # missing space after 申し込む / 登録 / 店長登録 / 店舗作成 / 希望休 / 給与
    t2 = re.sub(
        r"^(申し込む|申込む|申込|登録|店長登録|店舗作成|希望休|給与|名前)"
        r"(?=[^\s])",
        r"\1 ",
        t,
    )
    return t2.strip()


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _catalog_for(role: Role) -> list[tuple[str, tuple[str, ...]]]:
    if role == "guest":
        return _COMMON + _GUEST
    if role == "manager":
        return _COMMON + _MANAGER + _STAFF  # manager can also use staff cmds
    return _COMMON + _STAFF


def suggest_command(text: str, *, role: Role) -> str | None:
    """Return the closest canonical command, or None if nothing is close enough."""
    norm = normalize_for_match(text)
    if not norm or len(norm) > 40:
        return None
    # Exact / prefix after normalization (e.g. typo-fixed 登録)
    for canonical, aliases in _catalog_for(role):
        for a in aliases:
            if norm == a or norm == canonical:
                return canonical
            if norm.startswith(a + " ") or norm.startswith(canonical + " "):
                return canonical

    best: str | None = None
    best_dist = 99
    for canonical, aliases in _catalog_for(role):
        for candidate in {canonical, *aliases}:
            d = levenshtein(norm, candidate)
            # Allow ~25% edits, at least 1, at most 2 for short cmds / 3 for longer
            limit = 1 if len(candidate) <= 3 else (2 if len(candidate) <= 6 else 3)
            if d == 0:
                return canonical
            if d <= limit and d < best_dist:
                best_dist = d
                best = canonical
            # Also: input is candidate + extra fluff (見せてよ already stripped; シフト表ください)
            if candidate in norm and abs(len(norm) - len(candidate)) <= 3 and d <= limit + 1:
                if d < best_dist:
                    best_dist = d
                    best = canonical
    # Relative: don't suggest if input is totally different length
    if best and best_dist <= 3:
        return best
    return None


def detect_role(store: dict[str, Any] | None, user_id: str) -> Role:
    if not store:
        return "guest"
    members = store.get("members") or []
    for m in members:
        if m.get("user_id") == user_id:
            return "manager" if m.get("is_manager") else "staff"
    # fallback: line_user_ids membership without member row
    if user_id in (store.get("line_user_ids") or []):
        return "staff"
    return "guest"


def role_help_text(role: Role, *, store_name: str | None = None) -> str:
    """Concise role-aware help (explicit ヘルプ／使い方／？)."""
    header = "【使い方】ボタン優先です。下のボタンか、例のテキストで操作できます。"
    if role == "guest":
        return (
            f"{header}\n\n"
            "まだ店舗に登録されていません。\n"
            "■ できること\n"
            "・お店を始める（店長）→「店舗作成 青山店」\n"
            "・スタッフとして参加 →「登録 ○○○○」または「登録 ○○○○ 太郎」\n"
            "・既存コードで店長参加 →「店長登録 ○○○○」\n"
            "・「メニュー」「使い方」\n\n"
            "まずは下のボタンから選ぶのが簡単です。"
        )
    if role == "staff":
        sn = f"（{store_name}）" if store_name else ""
        return (
            f"{header}\n\n"
            f"■ スタッフメニュー{sn}\n"
            "・［出勤］［退勤］／「出勤」「退勤」\n"
            "・［シフト希望を出す］／「10/5 18:00〜22:00入れます」「10/6 NG」\n"
            "・［自分のシフト］［シフト表］／「自分のシフト」「シフト見せて」\n"
            "・［希望休］／「希望休 日曜」\n"
            "・「名前 太郎」「給与 今月」「自分の勤怠」\n"
            "・「メニュー」でボタン一覧\n\n"
            "わからないときは「メニュー」や「使い方」と送ってください。"
        )
    sn = f"（{store_name}）" if store_name else ""
    return (
        f"{header}\n\n"
        f"■ 店長メニュー{sn}\n"
        "・［店舗設定］［スタッフ管理］［スタッフ招待］\n"
        "・［シフト作成］／「シフト3案作って」\n"
        "・［条件設定］［提出状況］［条件で自動作成］\n"
        "・［人件費・給与］［今月の状況］［勤怠］\n"
        "・［料金プラン］／「申し込む プロ」\n"
        "・テキスト例: 「人件費を月20万円以内」「太郎は新人」\n"
        "・「メニュー」でボタン一覧\n\n"
        "スタッフ向けの出勤・シフト希望も使えます。\n"
        "わからないときは「メニュー」や「使い方」と送ってください。"
    )


def unknown_reply_text(
    raw: str,
    *,
    role: Role,
    store_name: str | None = None,
    suggestion: str | None = None,
) -> str:
    """Apology + optional もしかして + role-aware short suggestions."""
    shown = (raw or "").strip()
    if len(shown) > 40:
        shown = shown[:40] + "…"
    lines = [
        f"すみません、「{shown}」は分かりませんでした。",
    ]
    if suggestion:
        lines.append(f"もしかして：「{suggestion}」")
        lines.append("そのとおりなら、そのまま送るか下のボタンをタップしてください。")
    lines.append("")
    if role == "guest":
        lines.append("■ いま使える操作（未登録）")
        lines.append("・お店を始める（店長）／「店舗作成 店名」")
        lines.append("・スタッフとして参加／「登録 店舗コード」")
        lines.append("・「メニュー」「使い方」")
    elif role == "staff":
        sn = f"・所属: {store_name}" if store_name else ""
        if sn:
            lines.append(sn)
        lines.append("■ いま使える操作（スタッフ）")
        lines.append("・出勤／退勤／シフト希望を出す／自分のシフト")
        lines.append("・希望休／シフト見せて／名前 太郎／給与 今月")
        lines.append("・「メニュー」「使い方」")
    else:
        sn = f"・店舗: {store_name}" if store_name else ""
        if sn:
            lines.append(sn)
        lines.append("■ いま使える操作（店長）")
        lines.append("・店舗設定／スタッフ管理／シフト作成／条件設定")
        lines.append("・提出状況／条件で自動作成／人件費・給与／勤怠")
        lines.append("・料金プラン／スタッフ招待／「メニュー」「使い方」")
    lines.append("")
    lines.append("下のボタンから選ぶと確実です。")
    return "\n".join(lines)


def help_menu_items(role: Role) -> list[dict[str, Any]]:
    """Quick replies that always include メニュー + 使い方 (+ a few role actions)."""
    from line_ui import (
        encode_postback,
        guest_menu_items,
        manager_menu_items,
        qr_message,
        qr_postback,
        staff_menu_items,
    )

    help_btn = qr_postback("使い方", encode_postback(action="help"), display_text="使い方")
    menu_btn = qr_postback(
        "メニュー",
        encode_postback(action="manager_menu" if role == "manager" else ("staff_menu" if role == "staff" else "help")),
        display_text="メニュー",
    )
    if role == "guest":
        # guest_menu already has 使い方; ensure メニュー-like entry exists
        items = guest_menu_items()
        # prepend nothing; already has 使い方. Add explicit メニュー message tip.
        if not any((i.get("action") or {}).get("displayText") == "メニュー" for i in items):
            items = [qr_message("メニュー", "メニュー")] + items
        return items[:13]
    if role == "manager":
        base = manager_menu_items()
        # manager items are many; keep key ones + メニュー + 使い方 (LINE max 13)
        keep_labels = {"シフト作成", "条件設定", "提出状況", "人件費・給与", "料金プラン", "勤怠", "スタッフ招待"}
        trimmed = [i for i in base if (i.get("action") or {}).get("label") in keep_labels]
        return ([menu_btn] + trimmed + [help_btn])[:13]
    base = staff_menu_items()
    # staff_menu already has メニュー; append 使い方 if missing
    labels = {(i.get("action") or {}).get("label") for i in base}
    out = list(base)
    if "使い方" not in labels:
        out.append(help_btn)
    return out[:13]
