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

    スタッフ:
      登録 / 名前 / 希望休 / シフト見せて / 自分のシフト
    店長:
      店舗作成 店名 / 店長登録 CODE / シフト3案作って / 今週の人件費見せて
      人件費を下げて再計算 / 人件費 N円以内で組み直して / 人件費予算 N
      確定 [案] / Aさんは週20時間以内 / 時給 1200 / 割増 ...
      給与 今月 / 給与 太郎 / 給与確定 9月 / 給与明細 太郎 9月 / 給与CSV 9月
      交通費 / 手当 / 深夜時給 / 残業時給 / 実績 ...
    """
    raw = (text or "").strip()
    normalized = raw.replace("　", " ")

    # --- 販売版の注意事項・同意（店長機能のゲート） ---
    if re.fullmatch(r"注意事項|利用上の注意|免責", normalized, re.I):
        return {"intent": "show_terms", "raw": raw}
    if re.fullmatch(r"上記を確認しました|確認しました", normalized):
        return {"intent": "ack_terms", "raw": raw}
    if re.fullmatch(r"同意する|同意しました", normalized):
        return {"intent": "agree", "raw": raw}
    if re.fullmatch(r"確定する|給与を確定する", normalized):
        return {"intent": "payroll_lock_confirm", "raw": raw, "month": "今月"}

    # --- ボタンメニュー（テキストフォールバック） ---
    if re.fullmatch(r"メニュー|店長メニュー|管理メニュー", normalized):
        return {"intent": "manager_menu", "raw": raw}
    if re.fullmatch(r"スタッフメニュー|スタッフ用メニュー", normalized):
        return {"intent": "staff_menu", "raw": raw}
    if re.fullmatch(r"店舗設定", normalized):
        return {"intent": "store_settings", "raw": raw}
    if re.fullmatch(r"スタッフ管理", normalized):
        return {"intent": "staff_mgmt", "raw": raw}
    if re.fullmatch(r"シフト作成|シフトをつくる|シフトを作る", normalized):
        return {"intent": "make_three_plans", "raw": raw}
    if re.fullmatch(r"人件費[・･]給与|人件費給与|給与メニュー", normalized):
        return {"intent": "payroll_menu", "raw": raw}
    if re.fullmatch(r"今月の状況|今月の状態|ダッシュボード", normalized):
        return {"intent": "month_status", "raw": raw}

    # --- 料金プラン / 申し込み（外部チェックアウト。LINE IAP 定期は使わない） ---
    if re.fullmatch(r"プラン|料金プラン|料金|サブスク|サブスクリプション", normalized):
        return {"intent": "show_plans", "raw": raw}
    m_sub = re.search(
        r"^(?:申し込む|申込む|申込|アップグレード|契約)"
        r"\s*(?:[:：]\s*)?"
        r"(?P<plan>スタンダード|標準|STANDARD|standard|プロ|PRO|pro|フリー|無料|FREE|free)?"
        r"\s*$",
        normalized,
        re.I,
    )
    if m_sub:
        raw_plan = (m_sub.group("plan") or "").strip()
        plan_map = {
            "スタンダード": "STANDARD", "標準": "STANDARD", "STANDARD": "STANDARD", "standard": "STANDARD",
            "プロ": "PRO", "PRO": "PRO", "pro": "PRO",
            "フリー": "FREE", "無料": "FREE", "FREE": "FREE", "free": "FREE",
        }
        plan = plan_map.get(raw_plan) if raw_plan else None
        if plan == "FREE":
            return {"intent": "subscribe_free", "raw": raw, "plan": "FREE"}
        if plan:
            return {"intent": "subscribe", "raw": raw, "plan": plan}
        return {
            "intent": "subscribe_incomplete",
            "raw": raw,
            "hint": "例: 「申し込む スタンダード」または「申し込む プロ」",
        }

    # --- 店長登録 ---
    m_mgr = re.search(
        r"^(?:店長登録|マネージャー登録|店長に登録)"
        r"\s*(?:[:：]\s*)?"
        r"(?P<code>[A-Za-z0-9]{3,16})"
        r"(?:\s+(?:名前\s+)?(?P<name>.+?))?"
        r"\s*$",
        normalized,
        re.I,
    )
    if m_mgr:
        return {
            "intent": "register_manager",
            "raw": raw,
            "invite_code": m_mgr.group("code").upper(),
            "display_name": (m_mgr.group("name") or "").strip() or None,
        }
    if re.search(r"^(?:店長登録|マネージャー登録)\s*$", normalized):
        return {
            "intent": "register_manager_incomplete",
            "raw": raw,
            "hint": "例: 「店長登録 DEMO01」",
        }

    # --- 店舗作成（SaaS オンボード: 自分の店舗を新規発行） ---
    m_create = re.search(
        r"^(?:店舗作成|店舗を作成|新規店舗|ストア作成)"
        r"\s*(?:[:：]\s*)?"
        r"(?P<name>.+?)"
        r"\s*$",
        normalized,
    )
    if m_create:
        name = (m_create.group("name") or "").strip()
        # 誤爆防止: 明らかにコマンドっぽい語は拒否
        if name and not re.search(r"^(登録|シフト|給与|希望休|ヘルプ|help)$", name, re.I):
            return {
                "intent": "create_store",
                "raw": raw,
                "store_name": name,
                "display_name": None,
            }
    if re.search(r"^(?:店舗作成|店舗を作成|新規店舗)\s*$", normalized):
        return {
            "intent": "create_store_incomplete",
            "raw": raw,
            "hint": "例: 「店舗作成 青山店」→ 招待コード付きで店長として開設",
        }

    # --- 給与 / 人件費予算 / CSV / 実績 / 交通費・手当・深夜時給・残業時給 ---
    # 給与CSV 9月
    m_csv = re.search(
        r"^(?:給与\s*CSV|給与CSV|給与csv)\s*(?P<month>.+?)?\s*$",
        normalized,
        re.I,
    )
    if m_csv:
        return {
            "intent": "payroll_csv",
            "raw": raw,
            "month": (m_csv.group("month") or "今月").strip() or "今月",
        }

    # 給与確定 9月
    m_lock = re.search(
        r"^(?:給与確定|給与を確定|給与ロック)\s*(?P<month>.+?)?\s*$",
        normalized,
    )
    if m_lock:
        return {
            "intent": "payroll_lock",
            "raw": raw,
            "month": (m_lock.group("month") or "今月").strip() or "今月",
        }

    # 給与明細 太郎 9月
    m_slip = re.search(
        r"^(?:給与明細|明細)\s+"
        r"(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s*"
        r"(?P<month>.+?)?\s*$",
        normalized,
    )
    if m_slip:
        return {
            "intent": "payroll_payslip",
            "raw": raw,
            "who": m_slip.group("who"),
            "month": (m_slip.group("month") or "今月").strip() or "今月",
        }

    # 給与 今月 / 給与 太郎
    m_pay = re.search(
        r"^(?:給与|給与見[込み]|給料)\s*(?P<arg>.+?)?\s*$",
        normalized,
    )
    if m_pay:
        arg = (m_pay.group("arg") or "").strip() or "今月"
        if arg in {"今月", "当月", "この月"} or re.match(
            r"^(?:20\d{2}[-/年])?(?:1[0-2]|0?[1-9])\s*月?$", arg
        ):
            return {"intent": "payroll_month", "raw": raw, "month": arg}
        # 人名
        who = re.sub(r"(さん|くん|ちゃん)$", "", arg).strip()
        return {"intent": "payroll_staff", "raw": raw, "who": who, "month": "今月"}

    # 人件費予算 200000
    m_budg = re.search(
        r"^(?:人件費予算|予算人件費|人件費の予算)\s*(?:を|は|:|：)?\s*"
        r"(?P<yen>[0-9][0-9,]*)\s*円?\s*$",
        normalized,
    )
    if m_budg:
        return {
            "intent": "set_labor_budget",
            "raw": raw,
            "budget_yen": int(m_budg.group("yen").replace(",", "")),
        }

    # 交通費 太郎 500 / 交通費 500
    m_com = re.search(
        r"^(?:交通費)\s+"
        r"(?:(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s+)?"
        r"(?P<yen>[0-9][0-9,]*)\s*円?\s*$",
        normalized,
    )
    if m_com:
        return {
            "intent": "set_commute",
            "raw": raw,
            "who": m_com.group("who"),
            "commute_allowance": int(m_com.group("yen").replace(",", "")),
        }

    # 手当 太郎 役職手当 5000 / 手当 太郎 役職手当 5000 月
    m_all = re.search(
        r"^(?:手当|各種手当)\s+"
        r"(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s+"
        r"(?P<name>[ぁ-んァ-ン一-龥ーA-Za-z0-9]+)\s+"
        r"(?P<yen>[0-9][0-9,]*)\s*円?\s*"
        r"(?P<typ>月|毎月|出勤|日|シフト)?\s*$",
        normalized,
    )
    if m_all:
        typ_raw = (m_all.group("typ") or "月").strip()
        typ = "per_shift" if typ_raw in {"出勤", "日", "シフト"} else "monthly"
        return {
            "intent": "set_allowance",
            "raw": raw,
            "who": m_all.group("who"),
            "allowance_name": m_all.group("name"),
            "amount": int(m_all.group("yen").replace(",", "")),
            "allowance_type": typ,
        }

    # 深夜時給 太郎 1500 / 残業時給 太郎 1500
    m_nw = re.search(
        r"^(?P<kind>深夜時給|残業時給)\s+"
        r"(?:(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s+)?"
        r"(?P<wage>[0-9][0-9,]*)\s*円?\s*$",
        normalized,
    )
    if m_nw:
        return {
            "intent": "set_night_wage" if m_nw.group("kind") == "深夜時給" else "set_ot_wage",
            "raw": raw,
            "who": m_nw.group("who"),
            "hourly_wage": int(m_nw.group("wage").replace(",", "")),
        }

    # 実績 太郎 80 / 実績 太郎 80時間 深夜8 残業4 / 実績時間 太郎 80
    m_act = re.search(
        r"^(?:実績(?:時間)?|実働)\s+"
        r"(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s*"
        r"(?P<body>.+)?\s*$",
        normalized,
    )
    if m_act:
        body = (m_act.group("body") or "").strip()
        hours = None
        night = None
        ot = None
        days = None
        month = "今月"
        m_h = re.search(r"(?<![深残])(?P<h>[0-9]+(?:\.[0-9]+)?)\s*時間?", body)
        if m_h and "深夜" not in body[: m_h.start() + 1]:
            # prefer explicit 時間 or leading number
            pass
        m_h2 = re.search(r"(?:^|\s)(?P<h>[0-9]+(?:\.[0-9]+)?)\s*(?:時間)", body)
        m_h3 = re.search(r"^(?P<h>[0-9]+(?:\.[0-9]+)?)\s*(?:時間)?(?:\s|$)", body)
        if m_h2:
            hours = float(m_h2.group("h"))
        elif m_h3:
            hours = float(m_h3.group("h"))
        m_n = re.search(r"深夜\s*(?P<n>[0-9]+(?:\.[0-9]+)?)\s*(?:時間)?", body)
        if m_n:
            night = float(m_n.group("n"))
        m_o = re.search(r"残業\s*(?P<o>[0-9]+(?:\.[0-9]+)?)\s*(?:時間)?", body)
        if m_o:
            ot = float(m_o.group("o"))
        m_d = re.search(r"(?:出勤|勤務)\s*(?P<d>[0-9]+)\s*日", body)
        if m_d:
            days = int(m_d.group("d"))
        m_m = re.search(r"(?P<m>(?:20\d{2}[-/年])?(?:1[0-2]|0?[1-9])\s*月|今月)", body)
        if m_m:
            month = m_m.group("m").strip()
        return {
            "intent": "set_actual_hours",
            "raw": raw,
            "who": m_act.group("who"),
            "actual_hours": hours,
            "night_hours": night,
            "ot_hours": ot,
            "work_days": days,
            "month": month,
        }

        # --- シフト3案 ---
    if re.search(r"シフト\s*3案|3案作|三案作|シフト案.*作", normalized):
        return {"intent": "make_three_plans", "raw": raw}

    # --- 人件費を下げて再計算 / 人件費 N円以内 ---
    m_budget = re.search(
        r"人件費\s*(?P<yen>[0-9][0-9,]*)\s*円\s*(?:以内|以下)?\s*(?:で)?(?:組み直|再組|再計算|作)",
        normalized,
    )
    if m_budget:
        yen = int(m_budget.group("yen").replace(",", ""))
        return {"intent": "replan_budget", "raw": raw, "budget_yen": yen}
    if re.search(r"人件費\s*を?\s*(?:下げ|下げて|削減|安く).*再|再計算.*人件費|人件費優先で", normalized):
        return {"intent": "replan_lower_cost", "raw": raw}

    # --- 今週の人件費 ---
    if re.search(r"人件費\s*(?:見せて|見せ|確認|いくら|教えて)|今週の人件費|予定人件費", normalized):
        return {"intent": "show_labor_cost", "raw": raw}

    # --- 確定 ---
    m_conf = re.search(
        r"^確定\s*(?P<sel>[A-Za-z0-9ぁ-んァ-ン一-龥]*)?\s*$",
        normalized,
    )
    if m_conf:
        return {
            "intent": "confirm_plan",
            "raw": raw,
            "selector": (m_conf.group("sel") or "").strip() or None,
        }

    # --- 週上限: 「Aさんは週20時間以内」「太郎 週20時間」 ---
    m_cap = re.search(
        r"(?P<who>[A-Za-z0-9ぁ-んァ-ン一-龥]+?)\s*(?:さん|くん|ちゃん)?\s*"
        r"(?:は|を|の)?\s*週\s*(?P<hours>[0-9]+(?:\.[0-9]+)?)\s*時間"
        r"(?:以内|以下|まで)?",
        normalized,
    )
    if m_cap and not re.search(r"希望休", normalized):
        return {
            "intent": "set_hour_cap",
            "raw": raw,
            "who": m_cap.group("who"),
            "max_hours_week": float(m_cap.group("hours")),
        }

    # --- 時給 ---
    # 「時給 1200」「時給 太郎 1500」「時給を1200円」「太郎の時給 1500」
    m_wage = re.search(
        r"^(?:時給)\s*(?:を|は)?\s*"
        r"(?P<wage>[0-9][0-9,]*)\s*円?\s*$",
        normalized,
    )
    if m_wage:
        return {
            "intent": "set_wage",
            "raw": raw,
            "who": None,
            "hourly_wage": int(m_wage.group("wage").replace(",", "")),
        }
    m_wage2 = re.search(
        r"^(?:時給)\s+"
        r"(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s*(?:を|は|の)?\s*"
        r"(?P<wage>[0-9][0-9,]*)\s*円?\s*$",
        normalized,
    )
    if m_wage2:
        return {
            "intent": "set_wage",
            "raw": raw,
            "who": m_wage2.group("who"),
            "hourly_wage": int(m_wage2.group("wage").replace(",", "")),
        }
    m_wage3 = re.search(
        r"^(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*?)\s*"
        r"(?:さん|くん|ちゃん)?\s*の\s*時給\s*(?:を|は)?\s*"
        r"(?P<wage>[0-9][0-9,]*)\s*円?\s*$",
        normalized,
    )
    if m_wage3:
        return {
            "intent": "set_wage",
            "raw": raw,
            "who": m_wage3.group("who"),
            "hourly_wage": int(m_wage3.group("wage").replace(",", "")),
        }

    # --- 割増設定（店舗） ---
    m_prem = re.search(
        r"(?:割増|割増賃金)\s*(?P<kind>土日|週末|休日|祝日|深夜)\s*"
        r"(?P<rate>[0-9]+(?:\.[0-9]+)?)\s*倍?",
        normalized,
    )
    if m_prem:
        kind_map = {"土日": "weekend", "週末": "weekend", "休日": "weekend", "祝日": "holiday", "深夜": "night"}
        return {
            "intent": "set_premium",
            "raw": raw,
            "kind": kind_map.get(m_prem.group("kind"), "weekend"),
            "rate": float(m_prem.group("rate")),
        }

    # --- 役割 ---
    # 「役割 ホール」「役割 太郎 キッチン」「太郎の役割 キッチン」
    m_role = re.search(
        r"^(?:役割|スキル)\s*(?:を|は)?\s*(?P<role>[ぁ-んァ-ン一-龥ーA-Za-z0-9]+)\s*$",
        normalized,
    )
    if m_role:
        role = m_role.group("role").strip()
        if role and role not in {"変更", "設定"}:
            return {"intent": "set_role", "raw": raw, "who": None, "role": role}
    m_role2 = re.search(
        r"^(?:役割|スキル)\s+"
        r"(?P<who>[A-Za-zぁ-んァ-ン一-龥][A-Za-z0-9ぁ-んァ-ン一-龥]*)\s*"
        r"(?:さん|くん|ちゃん)?\s+"
        r"(?P<role>[ぁ-んァ-ン一-龥ーA-Za-z0-9]+)\s*$",
        normalized,
    )
    if m_role2:
        return {
            "intent": "set_role",
            "raw": raw,
            "who": m_role2.group("who"),
            "role": m_role2.group("role").strip(),
        }

    # --- 自分のシフト ---
    if re.search(r"自分のシフト|マイシフト|私のシフト", normalized):
        return {"intent": "show_own_shift", "raw": raw}

    # --- 表示名変更 ---
    m_name = re.search(
        r"^(?:名前(?:変更)?|表示名(?:変更)?|ニックネーム)"
        r"\s*(?:[:：]\s*)?"
        r"(?P<name>.+?)\s*$",
        normalized,
    )
    if m_name and not re.search(r"^登録", normalized):
        name = (m_name.group("name") or "").strip()
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

    # --- 登録 ---
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
        if dn and re.search(r"^(シフト|希望休|ヘルプ|使い方|help|確定|人件費)$", dn, re.I):
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

    # --- シフト見せて（3案・自分のシフトより後） ---
    if re.search(r"シフト|組表|スケジュール", normalized) and not re.search(
        r"希望休|3案|三案", normalized
    ):
        return {"intent": "show_shift", "raw": raw}

    # --- 希望休 ---
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

    if re.search(r"ヘルプ|使い方|help", normalized, re.I) or re.fullmatch(r"[?？]", normalized):
        return {"intent": "help", "raw": raw}

    # 未認識 → unknown（ヘルプ全文ダンプではなく、役割別サジェストへ）
    return {"intent": "unknown", "raw": raw}



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
        "ボタン優先です。「メニュー」と送るか下のボタンから操作できます。\n"
        "テキストコマンドも従来どおり使えます。\n"
        "\n"
        "■ スタッフ（ボタン／テキスト）\n"
        "・登録 → 「登録 DEMO01」／「登録 DEMO01 太郎」\n"
        "・名前 → 「名前 太郎」\n"
        "・希望休 → 曜日ボタン または「希望休 日曜」\n"
        "・自分のシフト／シフト表\n"
        "\n"
        "■ 店長メニュー\n"
        "店舗設定｜スタッフ管理｜シフト作成｜人件費・給与｜今月の状況｜料金プラン\n"
        "・シフト作成 → 希望優先／人件費優先／バランスの3カード＋［この案で確定］\n"
        "・「店舗作成 青山店」「店長登録 DEMO01」\n"
        "・「人件費予算 200000」「割増 土日 1.25」「太郎さんは週20時間以内」\n"
        "・「確定」「確定 希望」「確定 2」も可\n"
        "・「プラン」「料金プラン」「申し込む スタンダード」\n"
        "\n"
        "■ シフト希望・勤怠（スタッフ）\n"
        "・「10/5 18:00〜22:00入れます」「10/6 NG」／［シフト希望を出す］でカレンダー\n"
        "・［出勤］［退勤］で打刻（遅刻・早退は確定シフトと照合）\n"
        "\n"
        "■ 条件付き自動作成（店長）\n"
        "・「営業時間 10:00-22:00」「必要人数 夜 3」「人件費を月20万円以内」「連勤は4日まで」\n"
        "・「太郎は新人」「新人とベテランを必ず1人ずつ」「太郎と花子は同じ時間に入れない」\n"
        "・［提出状況］［条件でシフト作成］［今日の勤怠］「欠勤 太郎 10/5」\n"
        "・複数条件から最適化エンジン（古典ソルバ）がシフト候補を自動探索します\n"
        "\n"
        "■ 給与見込み（※振込なし）\n"
        "・「給与 今月」「給与確定 9月」「給与CSV 9月」\n"
        "・「交通費／手当／実績 …」はテキスト\n"
        "\n"
        f"{POC_BRANDING_COPY}\n"
        "※ 給与は見込み・明細・CSVまで。銀行振込は行いません。\n"
        "※ 組表本体は古典ソルバ（classical_greedy_heuristic）。"
        "QAOA は注目日比較時のみ明示します。\n"
        "※ 販売時はお客様の LINE 公式アカウントを使います。開発者個人 LINE は不要です。\n"
        "Credit: Powered by OpenQARP"
    )


def canned_follow_reply() -> str:
    return (
        "友だち追加ありがとうございます。\n"
        f"{POC_BRANDING_COPY}\n"
        "ボタンで操作できます（テキストも可）。\n"
        "まず店長から受け取った店舗コードで\n"
        "「登録 ○○○○」または「登録 ○○○○ 太郎」と送ってください。\n"
        "店長の方は「店舗作成 店舗名」で新規開設、\n"
        "または「店長登録 ○○○○」で既存コードに参加できます。\n"
        "登録後は「メニュー」→ 店舗設定｜スタッフ管理｜シフト作成｜人件費・給与｜今月の状況｜料金プラン。\n"
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
        # プランに応じたスタッフ数: メンバーの worker_id をシナリオ workers に反映
        member_workers = [w for w in labels.keys() if w]
        if member_workers:
            # keep stable order: A,B,C,D then W05...
            def _wid_key(w: str) -> tuple:
                if len(w) == 1 and w.isalpha():
                    return (0, w)
                return (1, w)
            ordered = sorted(set(member_workers), key=_wid_key)
            sc["workers"] = ordered
            # 基本シナリオ由来の希望休（例: D）が未参加枠を参照するとソルバ検証で落ちるため、
            # 実在する枠だけに絞り、最低人数も人数以内に丸める（2〜3名の新規店舗対策）。
            sc["preferred_offs"] = {
                w: list(v) for w, v in (sc.get("preferred_offs") or {}).items() if w in ordered
            }
            n = len(ordered)
            if n >= 1:
                sc["min_staff_per_day"] = max(1, min(int(sc.get("min_staff_per_day") or 1), n - 1 if n > 2 else n))
                if "qaoa_needed_on_focus" in sc:
                    sc["qaoa_needed_on_focus"] = max(1, min(int(sc["qaoa_needed_on_focus"]), n))
    # 予定人件費・制約用メタ（ソルバ本体は無視、plans が参照）
    sc["_store_id"] = store.get("store_id")
    return sc
