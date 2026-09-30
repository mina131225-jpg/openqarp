"""出勤／退勤の打刻（Asia/Tokyo）、遅刻・早退・欠勤の判定、給与「実績」への反映。

store["attendance"][YYYY-MM-DD][worker_id] = {
  "in": iso, "out": iso, "shift_start": "10:00", "shift_end": "16:00",
  "late_min": int, "early_min": int, "hours": float, "night_hours": float,
  "absent": bool, "absent_by": "manager"|"auto", "no_shift": bool
}
確定シフト（store["dated_shifts"]）と照合する。休憩控除はしない（PoC）。
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime, timedelta
from typing import Any

import shift_rules as sr
import slot_optimizer as so

LEGAL_WEEKLY_HOURS = 40.0


def _grace() -> int:
    try:
        return max(0, int(os.environ.get("LINE_LATE_GRACE_MIN", "0")))
    except ValueError:
        return 0


def _min_of(dt: datetime, base: date) -> int:
    """base 日の 0:00 からの経過分（翌日なら 24h 以上）。"""
    start = datetime(base.year, base.month, base.day, tzinfo=sr.JST)
    return int((dt - start).total_seconds() // 60)


def _night(in_min: int, out_min: int) -> float:
    total = 0
    for ns, ne in ((0, 5 * 60), (22 * 60, 29 * 60), (46 * 60, 53 * 60)):
        total += max(0, min(out_min, ne) - max(in_min, ns))
    return round(total / 60.0, 2)


def member_wid(store: dict[str, Any], user_id: str) -> str | None:
    for m in store.get("members") or []:
        if m.get("user_id") == user_id:
            return str(m.get("worker_id") or "") or None
    return None


def _open_record(store: dict[str, Any], wid: str, today: date) -> tuple[str, dict[str, Any]] | None:
    att = store.get("attendance") or {}
    for d in (today, today - timedelta(days=1)):
        rec = (att.get(d.isoformat()) or {}).get(wid)
        if rec and rec.get("in") and not rec.get("out"):
            return d.isoformat(), rec
    return None


def clock_in(store: dict[str, Any], user_id: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """store を直接更新（呼び出し側で update_store）。"""
    now = (now or sr.now_jst()).astimezone(sr.JST)
    wid = member_wid(store, user_id)
    if not wid:
        return False, "店舗に登録されていません。"
    today = now.date()
    day = store.setdefault("attendance", {}).setdefault(today.isoformat(), {})
    rec = day.get(wid) or {}
    if rec.get("in") and not rec.get("absent"):
        t = datetime.fromisoformat(rec["in"]).strftime("%H:%M")
        return False, f"本日はすでに {t} に出勤済みです。"
    win = so.worker_window(store, wid, today.isoformat())
    rec = {"in": now.isoformat(timespec="seconds")}
    now_min = _min_of(now, today)
    if win:
        rec["shift_start"], rec["shift_end"] = win
        late = now_min - sr.hm_to_min(win[0])
        rec["late_min"] = late if late > _grace() else 0
    else:
        rec["no_shift"] = True
    day[wid] = rec
    msg = f"出勤を記録しました {now.strftime('%m/%d %H:%M')}"
    if win:
        msg += f"（シフト {win[0]}〜{win[1]}）"
        if rec.get("late_min"):
            msg += f"\n⚠ 遅刻 {rec['late_min']}分"
    else:
        msg += "\n※ 本日の確定シフトはありません（シフト外の出勤として記録）"
    return True, msg


def clock_out(store: dict[str, Any], user_id: str, *, now: datetime | None = None) -> tuple[bool, str]:
    now = (now or sr.now_jst()).astimezone(sr.JST)
    wid = member_wid(store, user_id)
    if not wid:
        return False, "店舗に登録されていません。"
    found = _open_record(store, wid, now.date())
    if not found:
        return False, "出勤の記録がありません。先に「出勤」を押してください。"
    d, rec = found
    base = date.fromisoformat(d)
    rec["out"] = now.isoformat(timespec="seconds")
    in_min = _min_of(datetime.fromisoformat(rec["in"]), base)
    out_min = _min_of(now, base)
    rec["hours"] = round(max(0, out_min - in_min) / 60.0, 2)
    rec["night_hours"] = _night(in_min, out_min)
    if rec.get("shift_end"):
        early = sr.hm_to_min(rec["shift_end"]) - out_min
        rec["early_min"] = early if early > 0 else 0
    msg = f"退勤を記録しました {now.strftime('%m/%d %H:%M')}（勤務 {rec['hours']:g}時間）"
    if rec.get("early_min"):
        msg += f"\n⚠ 早退 {rec['early_min']}分（シフト終了 {rec['shift_end']}）"
    recompute_month_actuals(store, base.year, base.month)
    return True, msg


def mark_absent(store: dict[str, Any], wid: str, d: str, *, by: str = "manager", undo: bool = False) -> tuple[bool, str]:
    day = store.setdefault("attendance", {}).setdefault(d, {})
    rec = day.get(wid) or {}
    if undo:
        if rec.get("absent"):
            day.pop(wid, None)
            base = date.fromisoformat(d)
            recompute_month_actuals(store, base.year, base.month)
            return True, f"{sr.label_of(store, wid)} の {sr.date_label(d)} の欠勤を取り消しました。"
        return False, "欠勤の記録がありません。"
    if rec.get("in"):
        return False, f"{sr.label_of(store, wid)} は {sr.date_label(d)} に出勤記録があります。"
    win = so.worker_window(store, wid, d)
    day[wid] = {"absent": True, "absent_by": by, "shift_start": win[0] if win else None, "shift_end": win[1] if win else None,
                "marked_at": sr.now_jst().isoformat(timespec="seconds")}
    base = date.fromisoformat(d)
    recompute_month_actuals(store, base.year, base.month)
    return True, f"{sr.label_of(store, wid)} の {sr.date_label(d)} を欠勤にしました。"


def sweep_absences(store: dict[str, Any], *, now: datetime | None = None, lookback_days: int = 31) -> int:
    """シフト終了を過ぎても出勤記録が無い → 自動で欠勤（absent_by=auto）。"""
    now = (now or sr.now_jst()).astimezone(sr.JST)
    n = 0
    touched: set[tuple[int, int]] = set()
    for d, day in (store.get("dated_shifts") or {}).items():
        dd = date.fromisoformat(d)
        if dd > now.date() or dd < now.date() - timedelta(days=lookback_days):
            continue
        wids = {w for s in day.get("slots") or [] for w in s.get("workers") or []}
        for wid in wids:
            rec = ((store.get("attendance") or {}).get(d) or {}).get(wid)
            if rec:
                continue
            win = so.worker_window(store, wid, d)
            if win and _min_of(now, dd) >= sr.hm_to_min(win[1]):
                store.setdefault("attendance", {}).setdefault(d, {})[wid] = {
                    "absent": True, "absent_by": "auto", "shift_start": win[0], "shift_end": win[1],
                    "marked_at": now.isoformat(timespec="seconds")}
                n += 1
                touched.add((dd.year, dd.month))
    for y, m in touched:
        recompute_month_actuals(store, y, m)
    return n


def month_actuals(store: dict[str, Any], year: int, month: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    week_hours: dict[tuple[str, tuple[int, int]], float] = {}
    last = calendar.monthrange(year, month)[1]
    for day in range(1, last + 1):
        d = date(year, month, day)
        for wid, rec in ((store.get("attendance") or {}).get(d.isoformat()) or {}).items():
            row = out.setdefault(wid, {"actual_hours": 0.0, "night_hours": 0.0, "ot_hours": 0.0, "work_days": 0,
                                       "late_count": 0, "early_count": 0, "absent_count": 0, "clock_days": 0})
            if rec.get("absent"):
                row["absent_count"] += 1
                continue
            if rec.get("in") and rec.get("out"):
                row["actual_hours"] += float(rec.get("hours") or 0)
                row["night_hours"] += float(rec.get("night_hours") or 0)
                row["work_days"] += 1
                row["clock_days"] += 1
                wk = (wid, d.isocalendar()[:2])
                week_hours[wk] = week_hours.get(wk, 0.0) + float(rec.get("hours") or 0)
            if rec.get("late_min"):
                row["late_count"] += 1
            if rec.get("early_min"):
                row["early_count"] += 1
    for (wid, _), h in week_hours.items():
        out[wid]["ot_hours"] += max(0.0, h - LEGAL_WEEKLY_HOURS)
    for row in out.values():
        for k in ("actual_hours", "night_hours", "ot_hours"):
            row[k] = round(row[k], 2)
    return out


def recompute_month_actuals(store: dict[str, Any], year: int, month: int) -> None:
    """打刻集計を給与の実績（actual_hours_by_month）へ反映。手入力（source=manual）と確定済み月は上書きしない。"""
    key = f"{year:04d}-{month:02d}"
    pm = (store.get("payroll_months") or {}).get(key)
    if isinstance(pm, dict) and pm.get("locked"):
        return
    agg = month_actuals(store, year, month)
    month_map = store.setdefault("actual_hours_by_month", {}).setdefault(key, {})
    for wid, row in agg.items():
        cur = month_map.get(wid) or {}
        if cur.get("source") == "manual":
            continue
        if row["clock_days"] == 0 and not cur:
            continue
        month_map[wid] = {
            "actual_hours": row["actual_hours"],
            "night_hours": row["night_hours"],
            "ot_hours": row["ot_hours"],
            "work_days": row["work_days"],
            "source": "clock",
            "clock_days": row["clock_days"],
            "late_count": row["late_count"],
            "early_count": row["early_count"],
            "absent_count": row["absent_count"],
            "updated_at": sr.now_jst().isoformat(timespec="seconds"),
        }


def _fmt_time(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone(sr.JST).strftime("%H:%M") if iso else "—"


def day_status_lines(store: dict[str, Any], d: str) -> list[str]:
    att = (store.get("attendance") or {}).get(d) or {}
    planned = {w for s in ((store.get("dated_shifts") or {}).get(d) or {}).get("slots") or [] for w in s.get("workers") or []}
    lines = []
    for wid in sorted(planned | set(att), key=lambda w: sr.label_of(store, w)):
        rec = att.get(wid) or {}
        win = so.worker_window(store, wid, d)
        shift = f"{win[0]}〜{win[1]}" if win else "シフト外"
        if rec.get("absent"):
            st = "欠勤" + ("（自動）" if rec.get("absent_by") == "auto" else "")
        elif rec.get("in"):
            st = f"出勤 {_fmt_time(rec.get('in'))} / 退勤 {_fmt_time(rec.get('out'))}"
            flags = []
            if rec.get("late_min"):
                flags.append(f"遅刻{rec['late_min']}分")
            if rec.get("early_min"):
                flags.append(f"早退{rec['early_min']}分")
            if flags:
                st += "（" + "・".join(flags) + "）"
        else:
            st = "未出勤"
        lines.append(f"{sr.label_of(store, wid)}: {shift} … {st}")
    return lines


def format_day_text(store: dict[str, Any], d: str) -> str:
    lines = day_status_lines(store, d)
    head = f"【勤怠】{sr.date_label(d)}"
    return head + "\n" + ("\n".join(lines) if lines else "確定シフト・打刻はありません。")


def format_own_month_text(store: dict[str, Any], wid: str, year: int, month: int) -> str:
    row = month_actuals(store, year, month).get(wid)
    lines = [f"【あなたの勤怠】{year}年{month}月"]
    if not row:
        lines.append("まだ打刻がありません。")
    else:
        lines.append(f"出勤 {row['work_days']}日 ／ 実働 {row['actual_hours']:g}時間（深夜 {row['night_hours']:g}h・残業 {row['ot_hours']:g}h）")
        lines.append(f"遅刻 {row['late_count']}回 ／ 早退 {row['early_count']}回 ／ 欠勤 {row['absent_count']}回")
    last = calendar.monthrange(year, month)[1]
    for day in range(1, last + 1):
        d = date(year, month, day).isoformat()
        rec = ((store.get("attendance") or {}).get(d) or {}).get(wid)
        if rec:
            if rec.get("absent"):
                lines.append(f"{sr.date_label(d)} 欠勤")
            else:
                lines.append(f"{sr.date_label(d)} {_fmt_time(rec.get('in'))}〜{_fmt_time(rec.get('out'))}"
                             + (f" 遅刻{rec['late_min']}分" if rec.get("late_min") else "")
                             + (f" 早退{rec['early_min']}分" if rec.get("early_min") else ""))
    return "\n".join(lines)
