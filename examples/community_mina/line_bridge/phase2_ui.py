"""シフト希望・条件設定・条件付き自動作成・勤怠の LINE UI（ボタン優先、テキストでも可）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import shift_rules as sr
from line_ui import encode_postback, qr_message, qr_postback, with_quick_reply


def _pb(label: str, **params: str) -> dict[str, Any]:
    return qr_postback(label, encode_postback(**params), display_text=label)


def avail_picker_messages(dates: list[str], *, today: date | None = None) -> list[dict[str, Any]]:
    t = today or sr.now_jst().date()
    items: list[dict[str, Any]] = [{
        "type": "action",
        "action": {
            "type": "datetimepicker", "label": "カレンダーで選ぶ", "data": encode_postback(action="avail_pick"),
            "mode": "date", "initial": dates[0], "min": t.isoformat(), "max": (t + timedelta(days=60)).isoformat(),
        },
    }]
    for d in dates:
        items.append(_pb(sr.date_label(d), action="avail_pick", date=d))
    items.append(_pb("自分の希望", action="avail_mine"))
    items.append(_pb("提出完了", action="avail_done"))
    body = (
        f"シフト希望を出す日を選んでください（対象: {sr.date_label(dates[0])}〜{sr.date_label(dates[-1])}）。\n"
        "テキストでもOK: 「10/5 18:00〜22:00入れます」「10/6 NG」（改行で複数日まとめて可）"
    )
    return [with_quick_reply({"type": "text", "text": body}, items[:13])]


def avail_options_messages(store: dict[str, Any], d: str) -> list[dict[str, Any]]:
    rules = sr.get_rules(store)
    items = [_pb("終日OK", action="avail_set", date=d, st="ok")]
    for s in rules["slots"]:
        items.append(_pb(f"{s['name']}{s['start'][:2]}-{s['end'][:2]}時OK"[:20], action="avail_set", date=d, st="ok", slot=s["name"]))
    items.append(_pb("NG（入れない）", action="avail_set", date=d, st="ng"))
    items.append(_pb("別の日", action="avail_menu"))
    body = (f"{sr.date_label(d)} はどうしますか？\n"
            f"時間を指定する場合は「{int(d[5:7])}/{int(d[8:10])} 18:00〜22:00入れます」と送ってください。")
    return [with_quick_reply({"type": "text", "text": body}, items[:13])]


def rules_messages(store: dict[str, Any]) -> list[dict[str, Any]]:
    rules = sr.get_rules(store)
    items = []
    for s in rules["slots"][:3]:
        items.append(_pb(f"{s['name']}+1人", action="rule_req", slot=s["name"], d="1"))
        items.append(_pb(f"{s['name']}-1人", action="rule_req", slot=s["name"], d="-1"))
    items.append(_pb("新人ベテラン ON" if not rules.get("require_mix") else "新人ベテラン OFF",
                     action="rule_mix", on="0" if rules.get("require_mix") else "1"))
    items.append(_pb("連勤上限4日", action="rule_consec", n="4"))
    items.append(_pb("連勤上限5日", action="rule_consec", n="5"))
    items.append(_pb("提出状況", action="avail_tally"))
    items.append(_pb("条件でシフト作成", action="rule_plans"))
    return [with_quick_reply({"type": "text", "text": sr.format_rules_text(store)}, items[:13])]


def tally_messages(text: str, *, has_missing: bool) -> list[dict[str, Any]]:
    items = []
    if has_missing:
        items.append(_pb("未提出者にリマインド", action="avail_remind"))
    items += [_pb("条件でシフト作成", action="rule_plans"), _pb("条件設定", action="rule_show")]
    return [with_quick_reply({"type": "text", "text": text}, items)]


def after_avail_items() -> list[dict[str, Any]]:
    return [_pb("続けて入力", action="avail_menu"), _pb("自分の希望", action="avail_mine"), _pb("提出完了", action="avail_done")]


def attendance_items(*, manager: bool) -> list[dict[str, Any]]:
    items = [_pb("出勤", action="clock_in"), _pb("退勤", action="clock_out"), _pb("自分の勤怠", action="att_mine")]
    if manager:
        items.append(_pb("今日の勤怠", action="att_today"))
    return items


def plans_quick_items() -> list[dict[str, Any]]:
    return [
        _pb("⚛️ 量子で比べる", action="qcompare", key="balance"),
        _pb("条件設定", action="rule_show"),
        _pb("提出状況", action="avail_tally"),
        qr_message("メニュー", "メニュー"),
    ]


def qcompare_items() -> list[dict[str, Any]]:
    return [_pb("詳細", action="qcompare_detail"), _pb("条件設定", action="rule_show"), qr_message("メニュー", "メニュー")]
