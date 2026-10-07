#!/usr/bin/env python3
"""ワンタップ「来月のシフトを作る」— 店長が考えずボタン1つで来月シフト完成。

本体は古典ソルバ（slot_optimizer）。量子は既存の小問題比較のみ（月全体を量子とは言わない）。
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import shift_rules as sr
import slot_optimizer as so
from billing import has_feature
from line_ui import encode_postback, flex_button_postback, with_quick_reply
from plans import POC_FOOTER
from shift_messages import POC_BRANDING_COPY


def period_label(dates: list[str]) -> str:
    if not dates:
        return "—"
    d0, d1 = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    if d0.year == d1.year and d0.month == d1.month:
        return f"{d0.year}年{d0.month}月（{d0.day}日〜{d1.day}日・{len(dates)}日間）"
    return f"{sr.date_label(dates[0])}〜{sr.date_label(dates[-1])}（{len(dates)}日間）"


def store_treating_missing_unavailable(store: dict[str, Any], dates: list[str]) -> dict[str, Any]:
    """未提出者を「提出済み・全日未記入＝入れない」扱いにした店舗コピー。"""
    st = copy.deepcopy(store)
    if not dates:
        return st
    t = sr.tally(st, dates)
    period = dates[0]
    done = st.setdefault("availability_done", {}).setdefault(period, {})
    for wid in t["missing"]:
        done[wid] = sr.now_jst().isoformat(timespec="seconds")
        # 明示的に全日 NG（提出済みで空白と同じ効果を eligibility が保証するが、分かりやすく NG も入れる）
        av = st.setdefault("availability", {}).setdefault(wid, {})
        for d in dates:
            if d not in av:
                av[d] = {"status": "ng", "start": None, "end": None}
    return st


def prefs_gate(store: dict[str, Any], dates: list[str]) -> dict[str, Any]:
    t = sr.tally(store, dates)
    return {
        "dates": dates,
        "submitted_n": len(t["submitted"]),
        "missing_n": len(t["missing"]),
        "missing": list(t["missing"]),
        "total_n": len(t["submitted"]) + len(t["missing"]),
        "complete": len(t["missing"]) == 0,
    }


def pick_best_plan(bundle: dict[str, Any]) -> dict[str, Any]:
    """バランス案を第一候補。同点なら違反少→人件費安。"""
    plans = list(bundle.get("plans") or [])
    if not plans:
        raise ValueError("plans empty")
    bal = next((p for p in plans if p.get("key") == "balance"), None)
    if bal:
        return bal

    def key(p: dict[str, Any]) -> tuple:
        m = p["metrics"]
        return (m["violation_total"], m["labor_cost"], -(m.get("pref_rate") or 0))

    return sorted(plans, key=key)[0]


def generate_month_plans(
    store: dict[str, Any],
    *,
    dates: list[str] | None = None,
    treat_missing_unavailable: bool = False,
    iters: int = 600,
    preview_days: int | None = None,
) -> dict[str, Any]:
    dates = list(dates or sr.next_month_dates())
    if preview_days is not None:
        dates = dates[: max(1, int(preview_days))]
    work = store_treating_missing_unavailable(store, dates) if treat_missing_unavailable else store
    bundle = so.generate_rule_plans(work, dates, iters=iters, seed=11)
    bundle["period_kind"] = "next_month" if preview_days is None else "week_preview"
    bundle["period_label"] = period_label(dates)
    bundle["treat_missing_unavailable"] = bool(treat_missing_unavailable)
    bundle["one_tap"] = True
    best = pick_best_plan(bundle)
    bundle["best_key"] = best["key"]
    return bundle


def staff_shift_lines(store: dict[str, Any], dated: dict[str, Any], wid: str) -> list[str]:
    lines = []
    for d in sorted(dated.keys()):
        day = dated[d] or {}
        mine = [s for s in day.get("slots") or [] if wid in (s.get("workers") or [])]
        if mine:
            parts = "、".join(f"{s['name']} {s['start']}〜{s['end']}" for s in mine)
            lines.append(f"{sr.date_label(d)} {parts}")
    return lines


def notify_staff_own_shifts(
    store: dict[str, Any],
    dated: dict[str, Any],
    *,
    period_label_s: str,
    push_fn,
) -> dict[str, Any]:
    """各スタッフに自分のシフトだけ push。戻り値は集計。"""
    sent = 0
    targets = 0
    for m in store.get("members") or []:
        uid = m.get("user_id")
        wid = str(m.get("worker_id") or "")
        if not uid or not wid:
            continue
        targets += 1
        lines = staff_shift_lines(store, dated, wid)
        if lines:
            body = (
                f"【シフト確定】「{store.get('store_name')}」{period_label_s}\n"
                f"{sr.label_of(store, wid)} さんのシフトです。\n"
                + "\n".join(lines[:40])
                + ("\n…" if len(lines) > 40 else "")
                + "\n「自分のシフト」で再確認／出勤・退勤はボタンで。"
            )
        else:
            body = (
                f"【シフト確定】「{store.get('store_name')}」{period_label_s}\n"
                f"{sr.label_of(store, wid)} さんへの割当はありません（休み）。"
            )
        out = push_fn(uid, [_text(body[:4900])])
        if out.get("sent") or out.get("ok"):
            sent += 1
    return {"targets": targets, "sent": sent}


def _text(body: str) -> dict[str, Any]:
    return {"type": "text", "text": body}


def _row(k: str, v: str, *, bold: bool = False, color: str = "#0f172a") -> dict[str, Any]:
    return {
        "type": "box", "layout": "horizontal", "margin": "sm",
        "contents": [
            {"type": "text", "text": k, "size": "xs", "color": "#64748b", "flex": 3, "wrap": True},
            {"type": "text", "text": v, "size": "xs", "color": color, "align": "end", "flex": 5, "wrap": True,
             **({"weight": "bold"} if bold else {})},
        ],
    }


def incomplete_prefs_messages(store: dict[str, Any], gate: dict[str, Any]) -> list[dict[str, Any]]:
    names = "、".join(sr.label_of(store, w) for w in gate["missing"][:8])
    if gate["missing_n"] > 8:
        names += f" 他{gate['missing_n'] - 8}名"
    body = (
        f"【来月のシフト】希望の提出がまだ完了していません。\n"
        f"対象: {period_label(gate['dates'])}\n"
        f"提出 {gate['submitted_n']}/{gate['total_n']}名 ／ 未提出 {gate['missing_n']}名"
        + (f"（{names}）" if names else "")
        + "\n\nどうしますか？"
    )
    items = [
        {"type": "button", "style": "secondary", "height": "sm",
         "action": {"type": "postback", "label": "未提出者に催促して待つ",
                    "data": encode_postback(action="one_tap_wait"), "displayText": "未提出者に催促して待つ"}},
        {"type": "button", "style": "primary", "height": "sm",
         "action": {"type": "postback", "label": "未提出は入れない扱いで作成",
                    "data": encode_postback(action="one_tap_force"), "displayText": "未提出は入れない扱いで作成"}},
        {"type": "button", "style": "secondary", "height": "sm",
         "action": {"type": "postback", "label": "提出状況を見る",
                    "data": encode_postback(action="avail_tally"), "displayText": "提出状況"}},
    ]
    flex = {
        "type": "flex",
        "altText": "来月シフト：希望未提出あり",
        "contents": {
            "type": "bubble", "size": "mega",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                {"type": "text", "text": "希望の提出待ち", "weight": "bold", "size": "md"},
                {"type": "text", "text": body, "size": "xs", "wrap": True, "margin": "md", "color": "#334155"},
            ]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": items},
        },
    }
    from line_ui import qr_postback
    qr = [
        qr_postback("催促して待つ", encode_postback(action="one_tap_wait"), display_text="未提出者に催促して待つ"),
        qr_postback("入れない扱いで作成", encode_postback(action="one_tap_force"), display_text="未提出は入れない扱いで作成"),
        qr_postback("提出状況", encode_postback(action="avail_tally"), display_text="提出状況"),
    ]
    return [with_quick_reply({"type": "text", "text": body}, qr), with_quick_reply(flex, qr)]


def best_plan_messages(store: dict[str, Any], bundle: dict[str, Any], *, qcompare: bool = True) -> list[dict[str, Any]]:
    best = next(p for p in bundle["plans"] if p["key"] == bundle["best_key"])
    m = best["metrics"]
    pl = bundle.get("period_label") or period_label(bundle["dates"])
    tip = (
        f"【来月のシフト案ができました】{pl}\n"
        f"{so.HONEST_COPY}\n"
        f"おすすめ: {best['label']}（ボタン1つで確定できます）"
    )
    body = [
        {"type": "text", "text": f"おすすめ案：{best['label']}", "weight": "bold", "size": "md", "color": "#1e40af"},
        {"type": "text", "text": pl, "size": "xs", "color": "#64748b", "margin": "sm"},
        {"type": "separator", "margin": "md"},
        _row("人件費", f"{m['labor_cost']:,}円", bold=True),
        _row("希望充足率", so.pref_rate_text(m)),
        _row("制約違反", f"{m['violation_total']}件（{so.violations_text(m)}）",
             color="#dc2626" if m["violation_total"] else "#16a34a", bold=True),
        _row("計算時間", f"{best['seconds']*1000:.0f} ms（実測）"),
        {"type": "text", "text": so.METHOD_JA + " ／ 量子計算は使っていません", "size": "xxs", "color": "#94a3b8", "wrap": True, "margin": "md"},
    ]
    if bundle.get("treat_missing_unavailable"):
        body.append({"type": "text", "text": "※ 未提出者は「入れない」扱いで作成しています", "size": "xxs", "color": "#b45309", "wrap": True, "margin": "sm"})
    # short schedule peek (first 5 days)
    peek = so.schedule_lines(store, bundle, best)[:5]
    if peek:
        body.append({"type": "separator", "margin": "md"})
        body.append({"type": "text", "text": "スケジュール（冒頭）", "size": "xs", "weight": "bold", "margin": "sm"})
        body.append({"type": "text", "text": "\n".join(peek) + ("\n…" if len(bundle["dates"]) > 5 else ""),
                     "size": "xxs", "wrap": True, "color": "#334155", "margin": "sm"})

    footer = [
        {"type": "button", "style": "primary", "height": "sm",
         "action": {"type": "postback", "label": "この案で確定",
                    "data": encode_postback(action="confirm_rule_plan", key=best["key"]),
                    "displayText": f"条件案 {best['label']} で確定"}},
        {"type": "button", "style": "secondary", "height": "sm",
         "action": {"type": "postback", "label": "3案を見る",
                    "data": encode_postback(action="one_tap_show3"),
                    "displayText": "3案を見る"}},
    ]
    if qcompare and has_feature(store, "qaoa_compare"):
        footer.append({"type": "button", "style": "secondary", "height": "sm",
                       "action": {"type": "postback", "label": "⚛️ 量子で比べる",
                                  "data": encode_postback(action="qcompare", key=best["key"]),
                                  "displayText": "⚛️ 量子で比べる"}})
    footer.append({"type": "text", "text": "※ ⚛️ 量子で比べるは1日・1枠の小問題の比較実験です（月全体ではありません）",
                   "size": "xxs", "color": "#94a3b8", "wrap": True, "margin": "sm"})

    flex = {
        "type": "flex",
        "altText": f"来月シフト案｜{best['label']}｜{POC_BRANDING_COPY}",
        "contents": {
            "type": "bubble", "size": "mega",
            "header": {"type": "box", "layout": "vertical", "paddingAll": "14px", "backgroundColor": "#eff6ff",
                       "contents": [
                           {"type": "text", "text": "来月のシフト（ワンタップ）", "weight": "bold", "size": "md"},
                           {"type": "text", "text": store.get("store_name") or "", "size": "xs", "color": "#64748b", "margin": "sm"},
                       ]},
            "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": footer},
        },
    }
    from line_ui import qr_postback
    qr = [
        qr_postback("この案で確定", encode_postback(action="confirm_rule_plan", key=best["key"]),
                    display_text=f"条件案 {best['label']} で確定"),
        qr_postback("3案を見る", encode_postback(action="one_tap_show3"), display_text="3案を見る"),
        qr_postback("ダッシュボード", encode_postback(action="month_status"), display_text="ダッシュボード"),
    ]
    return [with_quick_reply({"type": "text", "text": tip}, qr), with_quick_reply(flex, qr)]


def completion_card(
    store: dict[str, Any],
    bundle: dict[str, Any],
    plan: dict[str, Any],
    *,
    notify_detail: str,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    m = plan["metrics"]
    pl = bundle.get("period_label") or period_label(bundle["dates"])
    ms = elapsed_ms if elapsed_ms is not None else plan.get("seconds", 0) * 1000
    body = [
        {"type": "text", "text": "完成しました 🎉", "weight": "bold", "size": "lg", "color": "#166534"},
        {"type": "text", "text": f"「{store.get('store_name')}」{pl}",
         "size": "xs", "color": "#64748b", "margin": "sm", "wrap": True},
        {"type": "separator", "margin": "md"},
        _row("採用案", plan["label"], bold=True),
        _row("人件費", f"{m['labor_cost']:,}円", bold=True),
        _row("希望充足率", so.pref_rate_text(m)),
        _row("制約違反", f"{m['violation_total']}件", color="#dc2626" if m["violation_total"] else "#16a34a", bold=True),
        _row("計算時間", f"{ms:.0f} ms（実測・古典ソルバ）"),
        {"type": "text", "text": notify_detail, "size": "xxs", "color": "#64748b", "wrap": True, "margin": "md"},
        {"type": "text", "text": so.HONEST_COPY + "\n※ 量子計算は使っていません。", "size": "xxs", "color": "#94a3b8", "wrap": True, "margin": "sm"},
        {"type": "text", "text": POC_FOOTER, "size": "xxs", "color": "#94a3b8", "wrap": True, "margin": "md"},
    ]
    footer = [
        flex_button_postback("ダッシュボード", encode_postback(action="month_status"), style="primary", display_text="ダッシュボード"),
        flex_button_postback("今日の勤怠", encode_postback(action="att_today"), style="secondary", display_text="今日の勤怠"),
    ]
    if has_feature(store, "qaoa_compare"):
        footer.insert(1, flex_button_postback(
            "⚛️ 量子で比べる", encode_postback(action="qcompare", key=plan["key"]),
            style="secondary", display_text="⚛️ 量子で比べる",
        ))
    return {
        "type": "flex",
        "altText": f"シフト完成｜{pl}｜{POC_BRANDING_COPY}",
        "contents": {
            "type": "bubble", "size": "mega",
            "header": {"type": "box", "layout": "vertical", "paddingAll": "14px", "backgroundColor": "#ecfdf5",
                       "contents": [{"type": "text", "text": "シフト完成", "weight": "bold", "size": "md", "color": "#166534"}]},
            "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": footer},
        },
    }


def free_limit_messages() -> list[dict[str, Any]]:
    body = (
        "【来月のシフトを作る】はスタンダード以上の機能です。\n"
        "フリーでは翌月の先頭7日だけプレビューできます（確定も7日分のみ）。\n"
        "フルの来月一括作成は「プラン」からスタンダード／プロへ。"
    )
    from line_ui import qr_postback
    qr = [
        qr_postback("1週間プレビュー", encode_postback(action="one_tap_preview"), display_text="1週間プレビュー"),
        qr_postback("料金プラン", encode_postback(action="show_plans"), display_text="料金プラン"),
    ]
    flex = {
        "type": "flex", "altText": "来月シフトはスタンダード以上",
        "contents": {"type": "bubble", "size": "mega",
                     "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                         {"type": "text", "text": "来月一括作成は有料プラン", "weight": "bold"},
                         {"type": "text", "text": body, "size": "xs", "wrap": True, "margin": "md"},
                     ]},
                     "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": [
                         flex_button_postback("1週間プレビュー", encode_postback(action="one_tap_preview"), style="secondary", display_text="1週間プレビュー"),
                         flex_button_postback("料金プラン", encode_postback(action="show_plans"), style="primary", display_text="料金プラン"),
                     ]}},
    }
    return [with_quick_reply({"type": "text", "text": body}, qr), with_quick_reply(flex, qr)]
