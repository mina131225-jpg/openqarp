"""シフト結果 → LINE 向けテキスト／Flex Message の整形と意図パース。

実 LINE 資格情報は不要。Streamlit プレビュー・webhook デモ共用。
"""

from __future__ import annotations

import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_COMMUNITY = _HERE.parent
_PITCH = _COMMUNITY / "pitch"

for _p in (_PITCH, _COMMUNITY):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from solver_bridge import (  # noqa: E402
    default_scenario,
    load_scenario_json,
    run_classical_week,
    validate,
)

DAY_ALIASES: dict[str, str] = {
    "月": "月",
    "月曜": "月",
    "月曜日": "月",
    "火": "火",
    "火曜": "火",
    "火曜日": "火",
    "水": "水",
    "水曜": "水",
    "水曜日": "水",
    "木": "木",
    "木曜": "木",
    "木曜日": "木",
    "金": "金",
    "金曜": "金",
    "金曜日": "金",
    "土": "土",
    "土曜": "土",
    "土曜日": "土",
    "日": "日",
    "日曜": "日",
    "日曜日": "日",
}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def get_line_credentials() -> dict[str, str]:
    """環境変数または Streamlit secrets 相当の値を読む（呼び出し側で secrets を注入可）。"""
    return {
        "channel_secret": os.environ.get("LINE_CHANNEL_SECRET", "").strip(),
        "channel_access_token": os.environ.get(
            "LINE_CHANNEL_ACCESS_TOKEN", ""
        ).strip(),
        "user_id": os.environ.get("LINE_USER_ID", "").strip(),
    }


def inject_credentials(
    *,
    channel_secret: str | None = None,
    channel_access_token: str | None = None,
    user_id: str | None = None,
) -> None:
    """テスト／Streamlit から一時的に env へ載せる。"""
    if channel_secret is not None:
        os.environ["LINE_CHANNEL_SECRET"] = channel_secret
    if channel_access_token is not None:
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = channel_access_token
    if user_id is not None:
        os.environ["LINE_USER_ID"] = user_id


def connection_status(
    creds: dict[str, str] | None = None,
    *,
    force_demo: bool | None = None,
) -> str:
    """未設定 / デモモード / 接続済 のいずれか。

    - 未設定: secret・token ともに空
    - デモモード: LINE_DEMO_MODE 強制、または片方だけ欠けている
    - 接続済: secret と token の両方があり、かつデモ強制でない
      （userId はプッシュ時のみ必要。webhook 返信は token で足りる）
    """
    c = creds or get_line_credentials()
    demo = (
        _env_truthy("LINE_DEMO_MODE")
        if force_demo is None
        else force_demo
    )
    has_secret = bool(c.get("channel_secret"))
    has_token = bool(c.get("channel_access_token"))
    if demo:
        return "デモモード"
    if has_secret and has_token:
        return "接続済"
    if not has_secret and not has_token:
        return "未設定"
    # 片方だけ → 実送信できないのでデモ扱い
    return "デモモード"


def load_base_scenario(path: str | Path | None = None) -> dict[str, Any]:
    env_path = path or os.environ.get("LINE_SCENARIO_JSON", "").strip()
    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            p = (_HERE / p).resolve()
        if p.exists():
            return load_scenario_json(p.read_text(encoding="utf-8"))
    sample = _COMMUNITY / "shift_scenario_tiny.json"
    if sample.exists():
        return load_scenario_json(sample.read_text(encoding="utf-8"))
    return default_scenario()


def parse_user_intent(text: str) -> dict[str, Any]:
    """LINE テキストから意図を抽出。

    例:
      「シフト見せて」「シフト表」→ show_shift
      「希望休 日曜」「希望休 A 土」→ set_pref
      その他 → help
    """
    raw = (text or "").strip()
    normalized = raw.replace("　", " ")
    lower_hint = normalized

    if re.search(r"シフト|組表|スケジュール", lower_hint) and not re.search(
        r"希望休", lower_hint
    ):
        return {"intent": "show_shift", "raw": raw}

    # 希望休 <任意ワーカー> <曜日>
    m = re.search(
        r"希望休\s*(?:[:：]\s*)?"
        r"(?:(?P<worker>[A-Za-z0-9ぁ-んァ-ン一-龥]+)\s+)?"
        r"(?P<day>月曜日?|火曜日?|水曜日?|木曜日?|金曜日?|土曜日?|日曜日?|[月火水木金土日])",
        normalized,
    )
    if m:
        day_raw = m.group("day")
        day = DAY_ALIASES.get(day_raw, DAY_ALIASES.get(day_raw[0], None))
        worker = m.group("worker")
        return {
            "intent": "set_pref",
            "raw": raw,
            "worker": worker,
            "day": day,
            "day_raw": day_raw,
        }

    if re.search(r"希望休|休み希望|休希望", normalized):
        return {
            "intent": "set_pref_incomplete",
            "raw": raw,
            "hint": "例: 「希望休 日曜」または「希望休 A 土」",
        }

    if re.search(r"ヘルプ|使い方|help", normalized, re.I):
        return {"intent": "help", "raw": raw}

    return {"intent": "help", "raw": raw}


def apply_pref_to_scenario(
    scenario: dict[str, Any],
    *,
    worker: str | None,
    day: str | None,
    default_worker: str | None = None,
) -> tuple[dict[str, Any], str]:
    """希望休をシナリオに反映。戻り値 (scenario, note)。"""
    sc = deepcopy(scenario)
    workers: list[str] = list(sc.get("workers", []))
    prefs: dict[str, list[str]] = {
        w: list(v) for w, v in sc.get("preferred_offs", {}).items()
    }
    for w in workers:
        prefs.setdefault(w, [])

    if not day:
        return sc, "曜日が読み取れませんでした。"

    target = worker or default_worker or (workers[0] if workers else None)
    if target is None:
        return sc, "スタッフがいません。"
    if target not in workers:
        # 名前ゆれ: 先頭一致
        matched = next((w for w in workers if w.startswith(target) or target in w), None)
        if matched is None:
            return sc, f"スタッフ「{target}」が見つかりません（登録: {', '.join(workers)}）。"
        target = matched

    if day not in prefs[target]:
        prefs[target].append(day)
    sc["preferred_offs"] = prefs
    note = f"{target} の希望休に「{day}」を登録しました。"
    return sc, note


def run_shift_for_line(
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """古典ソルバで週次を組み、LINE 向けに使える結果を返す。"""
    sc = deepcopy(scenario) if scenario is not None else load_base_scenario()
    validate(sc)
    classical = run_classical_week(sc)
    return {"scenario": sc, "classical": classical}


def build_shift_text(result: dict[str, Any], *, header: str | None = None) -> str:
    """週次シフトのテキスト表（LINE テキストメッセージ用）。"""
    sc = result["scenario"]
    classical = result["classical"]
    schedule = classical["schedule"]
    days: list[str] = sc["days"]
    workers: list[str] = sc["workers"]
    prefs = sc.get("preferred_offs", {})

    lines: list[str] = []
    lines.append(header or "【今週のシフトたたき台】")
    lines.append(f"スコア {classical['score']:.0f} ／ 所要 {classical['seconds']*1000:.1f} ms")
    lines.append("")

    # ヘッダ行
    lines.append("スタ |" + "|".join(f"{d}" for d in days))
    lines.append("----+" + "+".join("---" for _ in days))
    for w in workers:
        cells = []
        for d_idx, day in enumerate(days):
            on = schedule[w][d_idx]
            mark = "出" if on else "休"
            if day in prefs.get(w, []):
                mark = "出⚠" if on else "休✓"
            cells.append(f"{mark}")
        lines.append(f"{w:4}|" + "|".join(f"{c:^3}" for c in cells))

    pref_ok = sum(1 for h in classical["pref_hits"] if h["granted"])
    pref_all = len(classical["pref_hits"])
    lines.append("")
    lines.append(f"希望休充足: {pref_ok}/{pref_all}")
    for h in classical["pref_hits"]:
        tag = "✓" if h["granted"] else "✗"
        lines.append(f"  {tag} {h['worker']}・{h['day']}")

    focus = sc.get("qaoa_focus_day", "日")
    lines.append("")
    lines.append(
        f"注目日({focus}) 出勤: {', '.join(classical['focus_on']) or '—'} ／ "
        f"休み: {', '.join(classical['focus_off']) or '—'}"
    )
    lines.append("")
    lines.append(
        "※ PoC たたき台です。店長確認前提。本体は古典ソルバ。"
        " Powered by OpenQARP"
    )
    return "\n".join(lines)


def build_shift_flex(result: dict[str, Any], *, alt_text: str | None = None) -> dict[str, Any]:
    """LINE Flex Message (bubble) ペイロード。

    Messaging API の messages[] 要素としてそのまま使える形式。
    """
    sc = result["scenario"]
    classical = result["classical"]
    schedule = classical["schedule"]
    days: list[str] = sc["days"]
    workers: list[str] = sc["workers"]
    prefs = sc.get("preferred_offs", {})
    pref_ok = sum(1 for h in classical["pref_hits"] if h["granted"])
    pref_all = len(classical["pref_hits"])

    # コンパクトな表（contents の box 群）
    header_cols = [
        {
            "type": "text",
            "text": " ",
            "size": "xs",
            "flex": 2,
            "weight": "bold",
        }
    ] + [
        {
            "type": "text",
            "text": d,
            "size": "xs",
            "flex": 1,
            "align": "center",
            "weight": "bold",
            "color": "#64748b",
        }
        for d in days
    ]

    body_rows: list[dict[str, Any]] = []
    for w in workers:
        cols = [
            {
                "type": "text",
                "text": w,
                "size": "xs",
                "flex": 2,
                "weight": "bold",
            }
        ]
        for d_idx, day in enumerate(days):
            on = schedule[w][d_idx]
            if day in prefs.get(w, []):
                text = "休✓" if not on else "出⚠"
                color = "#166534" if not on else "#92400e"
            else:
                text = "出" if on else "休"
                color = "#1e40af" if on else "#94a3b8"
            cols.append(
                {
                    "type": "text",
                    "text": text,
                    "size": "xs",
                    "flex": 1,
                    "align": "center",
                    "color": color,
                }
            )
        body_rows.append({"type": "box", "layout": "horizontal", "contents": cols, "margin": "sm"})

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "シフトたたき台",
                    "weight": "bold",
                    "size": "md",
                    "color": "#0f172a",
                },
                {
                    "type": "text",
                    "text": f"スコア {classical['score']:.0f} ／ 希望休 {pref_ok}/{pref_all}",
                    "size": "xs",
                    "color": "#64748b",
                    "margin": "sm",
                },
            ],
            "backgroundColor": "#f8fafc",
            "paddingAll": "12px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": header_cols},
                {"type": "separator", "margin": "sm"},
                *body_rows,
            ],
            "paddingAll": "12px",
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "PoC・店長確認前提 ／ 本体は古典 ／ Powered by OpenQARP",
                    "size": "xxs",
                    "color": "#94a3b8",
                    "wrap": True,
                }
            ],
            "paddingAll": "10px",
        },
    }

    return {
        "type": "flex",
        "altText": alt_text or "今週のシフトたたき台",
        "contents": bubble,
    }


def help_text() -> str:
    return (
        "【使い方】\n"
        "・「シフト見せて」→ 今週のたたき台を返信\n"
        "・「希望休 日曜」→ 先頭スタッフの希望休を登録して再組表\n"
        "・「希望休 A 土」→ スタッフ指定で希望休登録\n"
        "・「ヘルプ」→ この案内\n"
        "\n"
        "※ デモ／未設定時は実 LINE には送らず、ログと定型返信のみです。\n"
        "Credit: Powered by OpenQARP"
    )


def canned_follow_reply() -> str:
    return (
        "友だち追加ありがとうございます。\n"
        "店舗シフト PoC（OpenQARP 試作）です。\n"
        "「シフト見せて」または「希望休 日曜」と送ってみてください。\n"
        "（資格情報未設定時はデモ返信のみ）"
    )
