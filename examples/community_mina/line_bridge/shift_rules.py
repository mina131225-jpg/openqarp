"""シフト条件（営業時間・枠・必要人数・人件費上限・連勤・新人/ベテラン・同時NG）と
スタッフのシフト希望（日付×時間帯の 入れます／NG）の保存・解析・集計。

データはすべて店舗レコード内（店舗隔離）:
  store["shift_rules"] = {...}
  store["availability"][worker_id][YYYY-MM-DD] = {"status": "ok"|"ng", "start": "18:00"|None, "end": ...}
  store["availability_done"][period_start][worker_id] = iso  （「提出完了」）
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]

DEFAULT_RULES: dict[str, Any] = {
    "business_hours": ["10:00", "22:00"],
    "slots": [
        {"name": "昼", "start": "10:00", "end": "16:00", "required": 2},
        {"name": "夜", "start": "16:00", "end": "22:00", "required": 2},
    ],
    "slots_auto": True,          # 営業時間変更で昼/夜を自動分割
    "cost_cap": None,            # {"period": "month"|"week", "yen": int}
    "max_consecutive": 5,
    "require_mix": False,        # 新人とベテランを各枠に必ず1人ずつ以上
    "ng_pairs": [],              # [[wid, wid], ...]
    "period_days": 7,
    "max_slots_per_day": 1,      # 1人1日あたりの枠数
}
LEVELS = ("新人", "ベテラン")


def now_jst() -> datetime:
    return datetime.now(JST)


# ---- time helpers ----------------------------------------------------------------

def hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def min_to_hm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def slot_hours(slot: dict[str, Any]) -> float:
    return (hm_to_min(slot["end"]) - hm_to_min(slot["start"])) / 60.0


def night_hours(start: str, end: str) -> float:
    """22:00〜翌5:00 と重なる時間（深夜）。end は 24:00 超も可（例 26:00）。"""
    s, e = hm_to_min(start), hm_to_min(end)
    total = 0
    for ns, ne in ((0, 5 * 60), (22 * 60, 29 * 60)):
        total += max(0, min(e, ne) - max(s, ns))
    return total / 60.0


def date_label(d: str | date) -> str:
    dd = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{dd.month}/{dd.day}({WEEKDAYS_JA[dd.weekday()]})"


def next_month_dates(*, today: date | None = None) -> list[str]:
    """翌カレンダー月の全日（YYYY-MM-DD）。"""
    t = today or now_jst().date()
    if t.month == 12:
        y, m = t.year + 1, 1
    else:
        y, m = t.year, t.month + 1
    import calendar as _cal
    last = _cal.monthrange(y, m)[1]
    return [date(y, m, d).isoformat() for d in range(1, last + 1)]


def planning_dates(store: dict[str, Any] | None = None, *, today: date | None = None) -> list[str]:
    """次の計画期間（既定: 翌週月曜から7日）。"""
    rules = get_rules(store)
    t = today or now_jst().date()
    start = t + timedelta(days=(7 - t.weekday()) % 7 or 7)
    start_override = (rules.get("period_start") or "").strip()
    if start_override:
        try:
            so = date.fromisoformat(start_override)
            if so >= t - timedelta(days=6):
                start = so
        except ValueError:
            pass
    n = int(rules.get("period_days") or 7)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# ---- rules -------------------------------------------------------------------------

def get_rules(store: dict[str, Any] | None) -> dict[str, Any]:
    rules = deepcopy(DEFAULT_RULES)
    rules.update(deepcopy((store or {}).get("shift_rules") or {}))
    return rules


def _auto_slots(open_hm: str, close_hm: str, required: list[int] | None = None) -> list[dict[str, Any]]:
    o, c = hm_to_min(open_hm), hm_to_min(close_hm)
    mid = o + ((c - o) // 2 // 60) * 60
    if mid <= o:
        mid = o + (c - o) // 2
    req = required or [2, 2]
    return [
        {"name": "昼", "start": open_hm, "end": min_to_hm(mid), "required": req[0]},
        {"name": "夜", "start": min_to_hm(mid), "end": close_hm, "required": req[-1]},
    ]


def members_by_label(store: dict[str, Any]) -> dict[str, str]:
    """表示名 / worker_id → worker_id（「さん」なしでも一致）。"""
    out: dict[str, str] = {}
    for m in store.get("members") or []:
        wid = str(m.get("worker_id") or "")
        if not wid:
            continue
        out[wid] = wid
        dn = (m.get("display_name") or m.get("worker_alias") or "").strip()
        if dn:
            out[dn] = wid
            out[dn.removesuffix("さん")] = wid
    return out


def label_of(store: dict[str, Any], wid: str) -> str:
    for m in store.get("members") or []:
        if str(m.get("worker_id") or "") == wid:
            return (m.get("display_name") or m.get("worker_alias") or wid).strip() or wid
    return wid


def resolve_name(store: dict[str, Any], name: str) -> str | None:
    n = (name or "").strip().removesuffix("さん")
    return members_by_label(store).get(n)


_T = r"(\d{1,2})(?:[:：](\d{2}))?\s*時?"
_RANGE = re.compile(_T + r"\s*(?:[〜~\-－ー–]|から)\s*" + _T + r"(?:まで)?")


def _hm(h: str, m: str | None) -> str:
    return f"{int(h):02d}:{int(m or 0):02d}"


def _yen(num: str, man: str | None) -> int:
    v = float(num.replace(",", ""))
    return int(round(v * 10000)) if man else int(round(v))


def parse_rule_text(text: str) -> dict[str, Any] | None:
    """店長の日本語条件 → {"intent": "rule_*", ...}。該当しなければ None。"""
    t = (text or "").strip().replace("　", " ")
    if t in {"条件", "シフト条件", "条件設定", "条件一覧"}:
        return {"intent": "rule_show"}
    if t in {"条件でシフト作成", "条件で自動作成", "自動作成", "条件で再計算", "再計算"}:
        return {"intent": "rule_plans"}
    if t in {"提出状況", "希望の集計", "シフト希望の集計"}:
        return {"intent": "avail_tally"}
    if t == "条件リセット":
        return {"intent": "rule_reset"}
    m = re.match(r"^営業時間\s*[:：]?\s*" + _RANGE.pattern + r"$", t)
    if m:
        return {"intent": "rule_business_hours", "open": _hm(m.group(1), m.group(2)), "close": _hm(m.group(3), m.group(4))}
    m = re.match(r"^枠\s*(\S+?)\s+" + _RANGE.pattern + r"\s*(\d+)\s*人$", t)
    if m:
        return {"intent": "rule_slot", "name": m.group(1), "start": _hm(m.group(2), m.group(3)),
                "end": _hm(m.group(4), m.group(5)), "required": int(m.group(6))}
    m = re.match(r"^必要人数\s*(.+)$", t)
    if m:
        pairs = re.findall(r"(\S+?)\s*[:：は]?\s*(\d+)\s*人?(?:\s|$|、|,)", m.group(1) + " ")
        if pairs:
            return {"intent": "rule_required", "pairs": [(n, int(v)) for n, v in pairs]}
    m = re.match(r"^人件費を?\s*(?:[1１]?(月|週))\s*(?:に|で)?\s*([\d,.]+)\s*(万)?\s*円?\s*(?:以内|まで)", t)
    if m:
        return {"intent": "rule_cost_cap", "period": "month" if m.group(1) == "月" else "week",
                "yen": _yen(m.group(2), m.group(3))}
    if re.match(r"^人件費(?:の)?上限\s*(?:なし|解除)$", t):
        return {"intent": "rule_cost_cap", "period": None, "yen": None}
    m = re.match(r"^(?:最大)?(?:連勤|連続勤務)\s*(?:は|を)?\s*(\d+)\s*日?\s*(?:まで|以内)?$", t)
    if m:
        return {"intent": "rule_max_consecutive", "days": int(m.group(1))}
    if re.match(r"^新人とベテランを?(?:必ず)?\s*1\s*人ずつ(?:入れる)?$", t) or t == "新人ベテラン条件オン":
        return {"intent": "rule_mix", "on": True}
    if t in {"新人ベテラン条件なし", "新人ベテラン条件オフ", "新人ベテラン条件解除"}:
        return {"intent": "rule_mix", "on": False}
    m = re.match(r"^(.+?)と(.+?)(?:は|を)同じ時間に(?:入れない|しない|入れないで)$", t)
    if m:
        return {"intent": "rule_ng_pair", "a": m.group(1), "b": m.group(2), "on": True}
    m = re.match(r"^(.+?)と(.+?)(?:の組み合わせ|の同時NG)(?:を)?(?:解除|OK)$", t)
    if m:
        return {"intent": "rule_ng_pair", "a": m.group(1), "b": m.group(2), "on": False}
    m = re.match(r"^(.+?)(?:さん)?は(新人|ベテラン)$", t)
    if m:
        return {"intent": "rule_level", "who": m.group(1), "level": m.group(2)}
    return None


def apply_rule_intent(store: dict[str, Any], intent: dict[str, Any]) -> tuple[bool, str]:
    """store をその場で更新（呼び出し側が update_store で保存）。"""
    rules = store.setdefault("shift_rules", {})
    cur = get_rules(store)
    kind = intent["intent"]
    if kind == "rule_reset":
        store["shift_rules"] = {}
        return True, "条件を初期値に戻しました。"
    if kind == "rule_business_hours":
        o, c = intent["open"], intent["close"]
        if hm_to_min(c) <= hm_to_min(o):
            return False, "営業時間は開始 < 終了で指定してください（例: 営業時間 10:00-22:00）。"
        rules["business_hours"] = [o, c]
        if cur.get("slots_auto", True):
            req = [s.get("required", 2) for s in cur["slots"]] or [2, 2]
            rules["slots"] = _auto_slots(o, c, req)
            rules["slots_auto"] = True
        return True, f"営業時間を {o}〜{c} にしました。"
    if kind == "rule_slot":
        if hm_to_min(intent["end"]) <= hm_to_min(intent["start"]):
            return False, "枠の時間は開始 < 終了で指定してください。"
        slots = [s for s in cur["slots"] if s["name"] != intent["name"]]
        slots.append({"name": intent["name"], "start": intent["start"], "end": intent["end"], "required": intent["required"]})
        slots.sort(key=lambda s: hm_to_min(s["start"]))
        rules["slots"] = slots
        rules["slots_auto"] = False
        return True, f"枠「{intent['name']}」{intent['start']}〜{intent['end']} 必要{intent['required']}人 を設定しました。"
    if kind == "rule_required":
        slots = cur["slots"]
        names = {s["name"] for s in slots}
        done, bad = [], []
        for n, v in intent["pairs"]:
            if n in names and 0 <= v <= 20:
                for s in slots:
                    if s["name"] == n:
                        s["required"] = v
                done.append(f"{n}{v}人")
            else:
                bad.append(n)
        if not done:
            return False, f"枠が見つかりません: {'、'.join(bad)}（枠: {'、'.join(sorted(names))}）"
        rules["slots"] = slots
        return True, "必要人数を更新しました: " + "、".join(done)
    if kind == "rule_cost_cap":
        if intent.get("yen"):
            rules["cost_cap"] = {"period": intent["period"], "yen": int(intent["yen"])}
            return True, f"人件費上限を{'月' if intent['period'] == 'month' else '週'} {int(intent['yen']):,}円 にしました。"
        rules["cost_cap"] = None
        return True, "人件費上限を解除しました。"
    if kind == "rule_max_consecutive":
        d = int(intent["days"])
        if not 1 <= d <= 14:
            return False, "連勤上限は 1〜14 日で指定してください。"
        rules["max_consecutive"] = d
        return True, f"連勤上限を {d} 日にしました。"
    if kind == "rule_mix":
        rules["require_mix"] = bool(intent["on"])
        if intent["on"]:
            return True, "各枠に「新人」と「ベテラン」を必ず1人ずつ以上入れる条件をオンにしました。\n（「太郎は新人」「花子はベテラン」で属性を設定）"
        return True, "新人／ベテランの条件をオフにしました。"
    if kind == "rule_ng_pair":
        a, b = resolve_name(store, intent["a"]), resolve_name(store, intent["b"])
        if not a or not b or a == b:
            return False, f"スタッフが見つかりません: {intent['a']} / {intent['b']}（表示名で指定してください）"
        pair = sorted([a, b])
        pairs = [sorted(p) for p in cur.get("ng_pairs") or [] if sorted(p) != pair]
        if intent["on"]:
            pairs.append(pair)
        rules["ng_pairs"] = pairs
        la, lb = label_of(store, a), label_of(store, b)
        return True, (f"「{la}」と「{lb}」を同じ時間に入れない条件を追加しました。" if intent["on"]
                      else f"「{la}」と「{lb}」の同時NGを解除しました。")
    if kind == "rule_level":
        wid = resolve_name(store, intent["who"])
        if not wid:
            return False, f"スタッフ「{intent['who']}」が見つかりません。"
        for m in store.get("members") or []:
            if str(m.get("worker_id") or "") == wid:
                m["level"] = intent["level"]
        return True, f"{label_of(store, wid)} を「{intent['level']}」に設定しました。"
    return False, "不明な条件です。"


def format_rules_text(store: dict[str, Any]) -> str:
    r = get_rules(store)
    lines = ["【シフト条件】"]
    bh = r.get("business_hours") or ["—", "—"]
    lines.append(f"営業時間: {bh[0]}〜{bh[1]}")
    for s in r["slots"]:
        lines.append(f"枠 {s['name']}: {s['start']}〜{s['end']} 必要 {s['required']}人")
    cap = r.get("cost_cap")
    lines.append("人件費上限: " + (f"{'月' if cap['period'] == 'month' else '週'} {int(cap['yen']):,}円" if cap else "なし"))
    lines.append(f"連勤上限: {r['max_consecutive']}日")
    lines.append("新人とベテランを1人ずつ: " + ("オン" if r.get("require_mix") else "オフ"))
    pairs = r.get("ng_pairs") or []
    lines.append("同じ時間NG: " + ("、".join(f"{label_of(store, a)}×{label_of(store, b)}" for a, b in pairs) if pairs else "なし"))
    levels = [f"{label_of(store, str(m.get('worker_id')))}={m.get('level')}" for m in store.get("members") or [] if m.get("level")]
    lines.append("属性: " + ("、".join(levels) if levels else "未設定（「太郎は新人」で設定）"))
    lines.append("")
    lines.append("テキストでも変更できます: 「営業時間 10:00-22:00」「必要人数 夜 3」「枠 深夜 22:00-26:00 1人」"
                 "「人件費を月20万円以内」「連勤は4日まで」「新人とベテランを必ず1人ずつ」「太郎と花子は同じ時間に入れない」")
    return "\n".join(lines)


# ---- availability ------------------------------------------------------------------

_NG_WORDS = re.compile(r"NG|ｎｇ|無理|不可|入れません|入れない|出れません|休み|×|✕|ダメ", re.I)
_OK_WORDS = re.compile(r"入れます|入れる|OK|ｏｋ|可能|出れます|出られます|大丈夫|◯|○|終日", re.I)
_DATE = re.compile(r"^(?:(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?|(明日|明後日|今日))\s*(?:[(（][月火水木金土日][)）])?\s*(.*)$")


def _resolve_date(month: int, day: int, today: date) -> date | None:
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today - timedelta(days=31):
            return d
    return None


def parse_availability_text(text: str, *, today: date | None = None) -> list[dict[str, Any]] | None:
    """「10/5 18:00〜22:00入れます」「10/6 NG」（改行・読点で複数可）→ entries。"""
    t0 = today or now_jst().date()
    raw = (text or "").strip().replace("　", " ")
    if not raw:
        return None
    segs = [s.strip() for s in re.split(r"[\n、,，／]+", raw) if s.strip()]
    out: list[dict[str, Any]] = []
    for seg in segs:
        m = _DATE.match(seg)
        if not m:
            return None
        if m.group(3):
            d = t0 + timedelta(days={"今日": 0, "明日": 1, "明後日": 2}[m.group(3)])
        else:
            d = _resolve_date(int(m.group(1)), int(m.group(2)), t0)
            if d is None:
                return None
        body = m.group(4) or ""
        rng = _RANGE.search(body)
        if _NG_WORDS.search(body) and not _OK_WORDS.search(body.replace("入れない", "")):
            out.append({"date": d.isoformat(), "status": "ng", "start": None, "end": None})
            continue
        if rng:
            s, e = _hm(rng.group(1), rng.group(2)), _hm(rng.group(3), rng.group(4))
            if hm_to_min(e) <= hm_to_min(s) or hm_to_min(e) > 29 * 60:
                return None
            out.append({"date": d.isoformat(), "status": "ok", "start": s, "end": e})
            continue
        if _OK_WORDS.search(body):
            out.append({"date": d.isoformat(), "status": "ok", "start": None, "end": None})
            continue
        return None
    return out or None


def set_availability(store: dict[str, Any], wid: str, entries: list[dict[str, Any]]) -> None:
    """同じ日に「昼OK」→「夜OK」のように連続・重なる時間帯が来たら1つの時間帯にまとめる。"""
    av = store.setdefault("availability", {}).setdefault(wid, {})
    for e in entries:
        start, end = e.get("start"), e.get("end")
        prev = av.get(e["date"])
        if (e["status"] == "ok" and start and prev and prev.get("status") == "ok" and prev.get("start")
                and hm_to_min(start) <= hm_to_min(prev["end"]) and hm_to_min(prev["start"]) <= hm_to_min(end)):
            start = min(start, prev["start"], key=hm_to_min)
            end = max(end, prev["end"], key=hm_to_min)
        av[e["date"]] = {"status": e["status"], "start": start, "end": end,
                         "updated_at": now_jst().isoformat(timespec="seconds")}


def mark_done(store: dict[str, Any], wid: str, period_start: str) -> None:
    store.setdefault("availability_done", {}).setdefault(period_start, {})[wid] = now_jst().isoformat(timespec="seconds")


def entry_text(e: dict[str, Any]) -> str:
    if e["status"] == "ng":
        return "NG"
    if e.get("start"):
        return f"{e['start']}〜{e['end']} 入れます"
    return "終日 入れます"


def worker_ids(store: dict[str, Any]) -> list[str]:
    return [str(m["worker_id"]) for m in store.get("members") or [] if m.get("worker_id")]


def submitted(store: dict[str, Any], wid: str, dates: list[str]) -> bool:
    av = (store.get("availability") or {}).get(wid) or {}
    done = ((store.get("availability_done") or {}).get(dates[0]) or {}).get(wid) if dates else None
    return bool(done) or any(d in av for d in dates)


def availability_for(store: dict[str, Any], wid: str, d: str) -> dict[str, Any] | None:
    return ((store.get("availability") or {}).get(wid) or {}).get(d)


def covers(entry: dict[str, Any] | None, slot: dict[str, Any]) -> bool:
    if not entry or entry.get("status") != "ok":
        return False
    if not entry.get("start"):
        return True
    return hm_to_min(entry["start"]) <= hm_to_min(slot["start"]) and hm_to_min(entry["end"]) >= hm_to_min(slot["end"])


def eligibility(store: dict[str, Any], wid: str, d: str, slot: dict[str, Any], dates: list[str]) -> str:
    """'ok'（希望あり）/'unknown'（未提出・可）/'no'（NG・時間外・提出済みで記載なし）。"""
    e = availability_for(store, wid, d)
    if e is not None:
        if e.get("status") == "ng":
            return "no"
        return "ok" if covers(e, slot) else "no"
    return "no" if submitted(store, wid, dates) else "unknown"


def own_availability_text(store: dict[str, Any], wid: str, dates: list[str]) -> str:
    lines = [f"【あなたのシフト希望】{date_label(dates[0])}〜{date_label(dates[-1])}"]
    for d in dates:
        e = availability_for(store, wid, d)
        lines.append(f"{date_label(d)}: {entry_text(e) if e else '未入力'}")
    lines.append("")
    lines.append("送り方の例: 「10/5 18:00〜22:00入れます」「10/6 NG」（改行で複数日まとめて可）")
    return "\n".join(lines)


def tally(store: dict[str, Any], dates: list[str]) -> dict[str, Any]:
    rules = get_rules(store)
    wids = worker_ids(store)
    subm = [w for w in wids if submitted(store, w, dates)]
    missing = [w for w in wids if w not in subm]
    days = []
    for d in dates:
        slots = []
        for s in rules["slots"]:
            ok = [w for w in wids if eligibility(store, w, d, s, dates) == "ok"]
            unk = [w for w in wids if eligibility(store, w, d, s, dates) == "unknown"]
            ng = [w for w in wids if (availability_for(store, w, d) or {}).get("status") == "ng"]
            slots.append({"slot": s["name"], "required": s["required"], "ok": ok, "unknown": unk, "ng": ng,
                          "short": max(0, s["required"] - len(ok))})
        days.append({"date": d, "slots": slots})
    return {"dates": dates, "submitted": subm, "missing": missing, "days": days}


def format_tally_text(store: dict[str, Any], t: dict[str, Any]) -> str:
    dates = t["dates"]
    n_all = len(t["submitted"]) + len(t["missing"])
    lines = [f"【シフト希望の集計】{date_label(dates[0])}〜{date_label(dates[-1])}",
             f"提出: {len(t['submitted'])}/{n_all}名"]
    lines.append("未提出: " + ("、".join(label_of(store, w) for w in t["missing"]) if t["missing"] else "なし 🎉"))
    lines.append("")
    lines.append("■ 日別・枠別（入れます人数／必要人数）")
    for day in t["days"]:
        parts = []
        for s in day["slots"]:
            mark = "✅" if s["short"] == 0 else "⚠"
            parts.append(f"{s['slot']} {len(s['ok'])}/{s['required']}{mark}")
        lines.append(f"{date_label(day['date'])}: " + "  ".join(parts))
    short_days = [f"{date_label(d['date'])}{s['slot']}" for d in t["days"] for s in d["slots"] if s["short"]]
    if short_days:
        lines.append("")
        lines.append("不足見込み: " + "、".join(short_days[:14]))
        lines.append("※ 未提出者は「入れるかもしれない」扱いで自動作成の候補に入ります（提出済みの人の未記入日は「入れない」扱い）。")
    return "\n".join(lines)
