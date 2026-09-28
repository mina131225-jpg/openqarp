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

# LINE 上でのブランド説明は、量子優位性をうたわず、PoC の位置づけを明示する。
POC_BRANDING_COPY = (
    "OpenQARP（量子アプリ）で試作した店舗シフトPoCです。"
    "組表本体は古典ソルバで、店長確認前提です。"
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
      「登録 MINA01」「登録 DEMO01 太郎」「登録 DEMO01 名前 太郎」→ register
      「名前 太郎」「名前変更 花子」→ set_name
      「シフト見せて」「シフト表」→ show_shift
      「希望休 日曜」「希望休 A 土」→ set_pref
      その他 → help
    """
    raw = (text or "").strip()
    normalized = raw.replace("　", " ")
    lower_hint = normalized

    # 表示名変更（登録済みユーザー向け）
    m_name = re.search(
        r"^(?:名前(?:変更)?|表示名(?:変更)?|ニックネーム)"
        r"\s*(?:[:：]\s*)?"
        r"(?P<name>.+?)\s*$",
        normalized,
    )
    if m_name and not re.search(r"^登録", normalized):
        name = (m_name.group("name") or "").strip()
        # 「名前」だけ／「名前変更」だけ
        if not name or name in {"変更", "を変更", "を設定"}:
            return {
                "intent": "set_name_incomplete",
                "raw": raw,
                "hint": "例: 「名前 太郎」または「名前変更 花子」",
            }
        return {
            "intent": "set_name",
            "raw": raw,
            "display_name": name,
        }

    # 登録 <店舗コード> [名前 <表示名> | <表示名>]
    m_reg = re.search(
        r"^(?:登録|バインド|紐付[けけ]?)"
        r"(?:\s*店舗(?:コード|ID)?)?"
        r"\s*(?:[:：]\s*)?"
        r"(?P<code>[A-Za-z0-9]{3,16})"
        r"(?:"
        r"\s+(?:名前|表示名)\s+(?P<name_kw>.+?)"
        r"|"
        r"\s+(?P<name_plain>.+?)"
        r")?"
        r"\s*$",
        normalized,
        re.I,
    )
    if m_reg:
        dn = (m_reg.group("name_kw") or m_reg.group("name_plain") or "").strip() or None
        # 誤って「登録 CODE シフト」等を名前にしないよう、予約語は無視
        if dn and re.search(r"^(シフト|希望休|ヘルプ|使い方|help)$", dn, re.I):
            dn = None
        return {
            "intent": "register",
            "raw": raw,
            "invite_code": m_reg.group("code").upper(),
            "display_name": dn,
        }
    if re.search(r"^(?:登録|バインド)\s*$", normalized) or re.search(
        r"^(?:登録|バインド)\s+店舗\s*$", normalized
    ):
        return {
            "intent": "register_incomplete",
            "raw": raw,
            "hint": (
                "例: 「登録 DEMO01」または「登録 DEMO01 太郎」"
                "（店長から受け取った店舗コード）"
            ),
        }

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
    label = None
    labels = sc.get("_display_labels") or {}
    if isinstance(labels, dict):
        label = labels.get(target)
    shown = label if label else target
    note = f"{shown} の希望休に「{day}」を登録しました。"
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
    lines.append(header or "【今週のシフト案】")
    lines.append(f"スコア {classical['score']:.0f} ／ 所要 {classical['seconds']*1000:.1f} ms")
    lines.append("")

    # ヘッダ行
    lines.append("スタ |" + "|".join(f"{d}" for d in days))
    lines.append("----+" + "+".join("---" for _ in days))
    labels = sc.get("_display_labels") or {}
    for w in workers:
        cells = []
        for d_idx, day in enumerate(days):
            on = schedule[w][d_idx]
            mark = "出" if on else "休"
            if day in prefs.get(w, []):
                mark = "出⚠" if on else "休✓"
            cells.append(f"{mark}")
        label = labels.get(w, w) if isinstance(labels, dict) else w
        # 全角想定で幅を揃える（最大4文字）
        lab = str(label)[:4]
        lines.append(f"{lab:4}|" + "|".join(f"{c:^3}" for c in cells))

    pref_ok = sum(1 for h in classical["pref_hits"] if h["granted"])
    pref_all = len(classical["pref_hits"])
    lines.append("")
    lines.append(f"希望休充足: {pref_ok}/{pref_all}")
    for h in classical["pref_hits"]:
        tag = "✓" if h["granted"] else "✗"
        hw = h["worker"]
        hlab = labels.get(hw, hw) if isinstance(labels, dict) else hw
        lines.append(f"  {tag} {hlab}・{h['day']}")

    focus = sc.get("qaoa_focus_day", "日")
    lines.append("")
    lines.append(
        f"注目日({focus}) 出勤: {', '.join(classical['focus_on']) or '—'} ／ "
        f"休み: {', '.join(classical['focus_off']) or '—'}"
    )
    lines.append("")
    lines.append(f"※ {POC_BRANDING_COPY} Powered by OpenQARP")
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

    labels = sc.get("_display_labels") or {}
    body_rows: list[dict[str, Any]] = []
    for w in workers:
        row_label = labels.get(w, w) if isinstance(labels, dict) else w
        cols = [
            {
                "type": "text",
                "text": str(row_label)[:8] or w,
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
                    "text": "今週のシフト案",
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
                    "text": (
                        "OpenQARP（量子アプリ）で試作した店舗シフトPoC ／ "
                        "組表本体は古典ソルバ・店長確認前提"
                    ),
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
        "altText": f"{alt_text or '今週のシフト案'}｜{POC_BRANDING_COPY}",
        "contents": bubble,
    }


def help_text() -> str:
    return (
        "【使い方】\n"
        "・「登録 DEMO01」→ 店長の店舗コードでスタッフ登録\n"
        "・「登録 DEMO01 太郎」→ 登録と同時に表示名を設定\n"
        "・「名前 太郎」／「名前変更 花子」→ 表示名の設定・変更\n"
        "・「シフト見せて」→ 所属店舗の今週のシフト案を返信\n"
        "・「希望休 日曜」→ 自分の枠に希望休を反映して再組表\n"
        "・「希望休 A 土」→ スタッフ枠を指定して希望休登録\n"
        "・「ヘルプ」→ この案内\n"
        "\n"
        f"{POC_BRANDING_COPY}\n"
        "※ 販売時はお客様の LINE 公式アカウントを使います。"
        "開発者個人 LINE は不要です。\n"
        "※ デモ／未設定時は実 LINE には送らず、ログと定型返信のみです。\n"
        "Credit: Powered by OpenQARP"
    )


def canned_follow_reply() -> str:
    return (
        "友だち追加ありがとうございます。\n"
        f"{POC_BRANDING_COPY}\n"
        "まず店長から受け取った店舗コードで\n"
        "「登録 ○○○○」または「登録 ○○○○ 太郎」と送ってください。\n"
        "表示名は後から「名前 太郎」でも設定できます。\n"
        "その後「シフト見せて」「希望休 日曜」が使えます。\n"
        "（資格情報未設定時はデモ返信のみ／開発者個人 LINE は不要）"
    )


def need_register_text() -> str:
    return (
        "まだ店舗に登録されていません。\n"
        "店長から受け取った店舗コードで\n"
        "「登録 ○○○○」または「登録 ○○○○ 太郎」と送ってください。\n"
        "例: 「登録 DEMO01」「登録 DEMO01 太郎」"
    )


def scenario_for_store(store: dict[str, Any] | None) -> dict[str, Any]:
    """店舗の preferences をベースシナリオに重ねた週次シナリオを返す。

    メンバーの display_name があれば _display_labels に載せて Flex／テキストで使う。
    """
    sc = load_base_scenario()
    if not store:
        return sc
    override = store.get("scenario_override")
    if isinstance(override, dict) and override.get("workers"):
        sc = deepcopy(override)
    prefs = store.get("preferences") or {}
    preferred = prefs.get("preferred_offs")
    if isinstance(preferred, dict):
        sc["preferred_offs"] = {
            w: list(v) for w, v in preferred.items()
        }
    # 表示名マップ（ソルバキーは A/B/C/D のまま）
    labels: dict[str, str] = {}
    for m in store.get("members") or []:
        wid = str(m.get("worker_id") or "").strip()
        if not wid:
            continue
        dn = (m.get("display_name") or m.get("worker_alias") or "").strip()
        labels[wid] = dn if dn else wid
    if labels:
        sc["_display_labels"] = labels
    return sc
