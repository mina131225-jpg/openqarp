#!/usr/bin/env python3
"""OpenQARP で試作した店舗シフト PoC — 営業向けデモ UI (Streamlit)。

本体のシフト最適化は古典ヒューリスティック。
比較・将来拡張に OpenQARP の量子アルゴリズム部品 (QAOA) を使う。
量子が古典に勝つとは主張しない。既存シフト SaaS の置き換えでもない。
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QARP_SKIP_ABI_CHECK", "1")

import pandas as pd
import streamlit as st

from solver_bridge import (
    default_scenario,
    load_scenario_json,
    qarp_available,
    run_full_demo,
)

HERE = Path(__file__).resolve().parent
PITCH_MD = HERE / "PITCH.md"
GO_LIVE_MD = HERE / "go_live.md"
SCENARIO_JSON = HERE.parent / "shift_scenario_tiny.json"
LINE_BRIDGE = HERE.parent / "line_bridge"
ONBOARDING_MD = LINE_BRIDGE / "ONBOARDING_CHECKLIST.md"

# LINE 連携（同梱 line_bridge）。未設置でも UI は落ちない。
_LINE_IMPORT_ERROR: Exception | None = None
try:
    import sys as _sys

    if str(LINE_BRIDGE) not in _sys.path:
        _sys.path.insert(0, str(LINE_BRIDGE))
    import notify as line_notify  # type: ignore
    import shift_messages as line_msgs  # type: ignore
    import stores as line_stores  # type: ignore
except Exception as _exc:  # noqa: BLE001
    _LINE_IMPORT_ERROR = _exc
    line_notify = None  # type: ignore
    line_msgs = None  # type: ignore
    line_stores = None  # type: ignore

st.set_page_config(
    page_title="OpenQARPで試作した店舗シフトPoC",
    page_icon="🗓️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---- スタイル（モバイル読みやすさ重視） ----
st.markdown(
    """
<style>
  .block-container {
    padding-top: 1.1rem !important;
    padding-bottom: 2rem !important;
    max-width: 1100px;
  }
  .hero-wrap {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 55%, #1d4ed8 100%);
    border-radius: 1rem; padding: 1.25rem 1.35rem 1.15rem; margin-bottom: 0.85rem;
    color: #f8fafc; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
  }
  .hero-kicker {
    font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase;
    color: #93c5fd; font-weight: 600; margin-bottom: 0.3rem;
  }
  .hero-title {
    font-size: clamp(1.35rem, 4.5vw, 1.85rem); font-weight: 800;
    line-height: 1.3; margin: 0 0 0.4rem;
  }
  .hero-sub { color: #cbd5e1; font-size: 0.95rem; margin: 0; line-height: 1.55; }
  .value-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.65rem;
    margin: 0.7rem 0 0.25rem;
  }
  .value-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 0.75rem;
    padding: 0.75rem 0.9rem; box-shadow: 0 1px 2px rgba(15,23,42,0.04);
  }
  .value-card b { display: block; color: #0f172a; font-size: 0.95rem; margin-bottom: 0.2rem; }
  .value-card span { color: #475569; font-size: 0.86rem; line-height: 1.45; }
  .ba-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;
    margin: 0.55rem 0 0.85rem;
  }
  .ba-card {
    border-radius: 0.75rem; padding: 0.85rem 1rem; min-height: 7.2rem;
  }
  .ba-before { background: #fff7ed; border: 1px solid #fed7aa; }
  .ba-after { background: #eff6ff; border: 1px solid #bfdbfe; }
  .ba-card h4 {
    margin: 0 0 0.45rem; font-size: 0.92rem; font-weight: 800; line-height: 1.35;
  }
  .ba-before h4 { color: #9a3412; }
  .ba-after h4 { color: #1e40af; }
  .ba-card ul {
    margin: 0; padding-left: 1.1rem; color: #334155;
    font-size: 0.86rem; line-height: 1.55;
  }
  .badge-ok {
    display: inline-block; background: #d1fae5; color: #065f46;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 700; font-size: 0.85rem;
  }
  .badge-ng {
    display: inline-block; background: #fee2e2; color: #991b1b;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 700; font-size: 0.85rem;
  }
  .badge-soft {
    display: inline-block; background: #f1f5f9; color: #334155;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 600; font-size: 0.82rem;
  }
  .disclaimer-box {
    background: #fffbeb; border-left: 4px solid #f59e0b;
    padding: 0.75rem 0.95rem; border-radius: 0.45rem; margin: 0.25rem 0 0.85rem;
    font-size: 0.9rem; color: #78350f; line-height: 1.5;
  }
  .product-note {
    background: #f0fdf4; border-left: 4px solid #22c55e;
    padding: 0.65rem 0.95rem; border-radius: 0.45rem; margin: 0.45rem 0 0.85rem;
    font-size: 0.88rem; color: #14532d; line-height: 1.5;
  }
  .metric-strip {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.65rem;
    margin: 0.45rem 0 0.85rem;
  }
  .metric-card {
    background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 0.75rem; padding: 0.75rem 0.9rem;
  }
  .metric-card .label { color: #64748b; font-size: 0.8rem; margin-bottom: 0.15rem; }
  .metric-card .value { color: #0f172a; font-size: 1.35rem; font-weight: 800; line-height: 1.2; }
  .metric-card .hint { color: #94a3b8; font-size: 0.76rem; margin-top: 0.15rem; }
  .compare-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 0.75rem;
    padding: 0.85rem 0.95rem; min-height: 8.5rem;
  }
  .compare-card h5 { margin: 0 0 0.5rem; font-size: 0.95rem; color: #1e293b; }
  .cta-box {
    background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 0.75rem;
    padding: 0.9rem 1rem; margin: 0.65rem 0 0.25rem; line-height: 1.55;
  }
  .oneclick-wrap {
    background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 0.85rem;
    padding: 0.95rem 1.05rem 1.05rem; margin: 0.2rem 0 0.75rem; text-align: center;
  }
  .oneclick-wrap .hint {
    color: #64748b; font-size: 0.88rem; margin: 0.45rem 0 0; line-height: 1.45;
  }
  .proposal-card {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 0.5rem;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.06);
    padding: 1.15rem 1.25rem 1.05rem;
    margin: 0.35rem 0 1rem;
    color: #0f172a;
  }
  .proposal-card .pc-head {
    display: flex; justify-content: space-between; align-items: baseline;
    border-bottom: 2px solid #0f172a; padding-bottom: 0.45rem; margin-bottom: 0.75rem;
    gap: 0.5rem; flex-wrap: wrap;
  }
  .proposal-card .pc-title {
    font-size: 1.15rem; font-weight: 800; letter-spacing: 0.02em; margin: 0;
  }
  .proposal-card .pc-sub {
    font-size: 0.78rem; color: #64748b; font-weight: 600;
  }
  .proposal-card .pc-store {
    font-size: 1.02rem; font-weight: 700; margin: 0 0 0.65rem; color: #1e3a5f;
  }
  .proposal-card .pc-section {
    margin: 0.55rem 0 0.15rem; font-size: 0.82rem; font-weight: 800;
    color: #334155; letter-spacing: 0.04em;
  }
  .proposal-card .pc-body {
    font-size: 0.92rem; line-height: 1.6; color: #1e293b; margin: 0 0 0.35rem;
  }
  .proposal-card .pc-cta {
    margin-top: 0.85rem; padding: 0.7rem 0.85rem;
    background: #eff6ff; border: 1px solid #93c5fd; border-radius: 0.45rem;
    font-size: 0.92rem; line-height: 1.55; color: #1e3a8a;
  }
  .proposal-card .pc-foot {
    margin-top: 0.65rem; font-size: 0.76rem; color: #94a3b8; line-height: 1.45;
  }
  div[data-testid="stDataFrame"] { font-size: 0.92rem; }
  div.stButton > button[kind="primary"],
  div.stButton > button[data-testid="baseButton-primary"] {
    font-size: 1.08rem !important;
    font-weight: 700 !important;
    padding: 0.65rem 1.2rem !important;
    min-height: 3rem;
  }
  @media (max-width: 900px) {
    .value-grid, .metric-strip, .ba-grid { grid-template-columns: 1fr; }
    .proposal-card { padding: 1rem 0.95rem; }
    .hero-wrap { padding: 1.05rem 1.05rem 1rem; }
  }
  @media print {
    .proposal-card { box-shadow: none; border: 1px solid #000; }
  }
</style>
""",
    unsafe_allow_html=True,
)


def _badge(ok: bool, yes: str = "一致", no: str = "不一致") -> str:
    cls = "badge-ok" if ok else "badge-ng"
    return f'<span class="{cls}">{yes if ok else no}</span>'


def schedule_to_dataframe(schedule: dict, scenario: dict) -> pd.DataFrame:
    days = scenario["days"]
    workers = scenario["workers"]
    prefs = scenario.get("preferred_offs", {})
    rows = []
    for w in workers:
        row: dict = {"スタッフ": w}
        for d_idx, day in enumerate(days):
            on = schedule[w][d_idx]
            cell = "出勤" if on else "休み"
            if day in prefs.get(w, []):
                cell = "出勤⚠希望" if on else "休み✓希望"
            row[day] = cell
        row["出勤日数"] = str(int(sum(schedule[w])))
        rows.append(row)
    daily: dict = {"スタッフ": "（人数）"}
    for d_idx, day in enumerate(days):
        daily[day] = str(sum(1 for w in workers if schedule[w][d_idx]))
    daily["出勤日数"] = "-"
    rows.append(daily)
    return pd.DataFrame(rows)


def style_schedule_df(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    def cell_color(val):
        if not isinstance(val, str):
            return ""
        if "休み✓" in val:
            return "background-color: #dcfce7; color: #166534; font-weight: 600;"
        if "出勤⚠" in val:
            return "background-color: #fef3c7; color: #92400e; font-weight: 600;"
        if val == "出勤":
            return "background-color: #eff6ff; color: #1e40af;"
        if val == "休み":
            return "background-color: #f8fafc; color: #64748b;"
        return ""

    day_cols = [c for c in df.columns if c not in ("スタッフ", "出勤日数")]
    styler = df.style.map(cell_color, subset=day_cols)
    styler = styler.set_properties(
        subset=["スタッフ"],
        **{"font-weight": "700", "background-color": "#f1f5f9"},
    )
    styler = styler.set_properties(
        subset=["出勤日数"],
        **{"text-align": "center", "font-weight": "600"},
    )
    return styler


def build_scenario_from_ui(
    workers_text: str,
    min_staff: int,
    max_consec: int,
    focus_day: str,
    need_on: int,
    pref_rows: list[dict],
) -> dict:
    workers = [w.strip() for w in workers_text.replace("、", ",").split(",") if w.strip()]
    days = ["月", "火", "水", "木", "金", "土", "日"]
    preferred_offs: dict[str, list[str]] = {w: [] for w in workers}
    for row in pref_rows:
        name = str(row.get("スタッフ", "")).strip()
        day = str(row.get("希望休", "")).strip()
        if name in preferred_offs and day in days:
            if day not in preferred_offs[name]:
                preferred_offs[name].append(day)
    return {
        "workers": workers,
        "days": days,
        "min_staff_per_day": min_staff,
        "max_consecutive_days": max_consec,
        "preferred_offs": preferred_offs,
        "qaoa_focus_day": focus_day,
        "qaoa_needed_on_focus": need_on,
    }


def load_sample_scenario() -> dict:
    if SCENARIO_JSON.exists():
        return load_scenario_json(SCENARIO_JSON.read_text(encoding="utf-8"))
    return default_scenario()


def render_proposal_card(result: dict) -> None:
    classical = result["classical"]
    quantum = result["quantum"]
    sc = result["scenario"]
    pref_ok = sum(1 for h in classical["pref_hits"] if h["granted"])
    pref_all = len(classical["pref_hits"])
    focus = sc.get("qaoa_focus_day", "日")
    focus_on = "、".join(classical["focus_on"]) or "—"
    focus_off = "、".join(classical["focus_off"]) or "—"

    if quantum.get("available"):
        agree = bool(quantum.get("agree_exact"))
        agree_txt = "一致" if agree else "不一致"
        q_line = (
            f"注目日（{focus}）の縮小比較で、古典厳密解と QAOA は「{agree_txt}」。"
            "勝ち主張ではなく並び確認です。"
        )
    else:
        q_line = (
            "今回は量子比較をスキップ（古典のみ）。"
            "OpenQARP 部品は同画面で後から並べられます。"
        )

    granted = [
        f"{h['worker']}・{h['day']}" for h in classical["pref_hits"] if h["granted"]
    ]
    denied = [
        f"{h['worker']}・{h['day']}" for h in classical["pref_hits"] if not h["granted"]
    ]
    pref_summary = f"希望休 {pref_ok}/{pref_all} 充足"
    if granted:
        pref_summary += f"（通った: {', '.join(granted)}）"
    if denied:
        pref_summary += f"／ 未充足: {', '.join(denied)}"

    workers_n = len(sc.get("workers", []))
    html = f"""
<div class="proposal-card">
  <div class="pc-head">
    <p class="pc-title">シフトたたき台・提案サマリー</p>
    <span class="pc-sub">印刷・画面共有向け ／ PoC</span>
  </div>
  <p class="pc-store">店名（仮）サンプルカフェ ○○店　｜　スタッフ {workers_n} 名・週次</p>

  <div class="pc-section">今週のポイント</div>
  <p class="pc-body">
    スコア <b>{classical['score']:.0f}</b>　／　{pref_summary}<br/>
    最低人数 {sc.get('min_staff_per_day')} 名・連続勤務上限 {sc.get('max_consecutive_days')} 日を満たすたたき台です。
  </p>

  <div class="pc-section">{focus}曜の決め方</div>
  <p class="pc-body">
    出勤 <b>{focus_on}</b>　／　休み <b>{focus_off}</b><br/>
    希望休のぶつかりを見ながら、必要人数を確保する案です（店長確認前提）。
  </p>

  <div class="pc-section">古典／量子の並び</div>
  <p class="pc-body">{q_line}</p>

  <div class="pc-cta">
    <b>お試し価格 CTA（仮）</b>：画面合わせ 0〜数万円／回 → 感触OKなら店舗カスタム（月額数万円〜・仮）。
    今日は感触確認でOK。正式見積は別途。
  </div>
  <p class="pc-foot">
    製品の中心は古典ソルバ。OpenQARP の QAOA は比較・将来拡張デモ。
    量子優位性は主張しません。既存シフト SaaS の置き換えではありません。<br/>
    Credit: Powered by OpenQARP
  </p>
</div>
"""
    st.markdown(html, unsafe_allow_html=True)



def _line_creds_from_ui(
    secret: str,
    token: str,
    user_id: str,
) -> dict[str, str]:
    """UI 入力 → st.secrets → 環境変数の順で埋める。"""
    def _secret_get(*keys: str) -> str:
        try:
            sec = st.secrets  # type: ignore[attr-defined]
        except Exception:
            return ""
        for k in keys:
            try:
                v = sec.get(k, "") if hasattr(sec, "get") else sec[k]
            except Exception:
                continue
            if v:
                return str(v).strip()
        return ""

    import os as _os

    channel_secret = (
        secret.strip()
        or _secret_get("LINE_CHANNEL_SECRET", "line_channel_secret")
        or _os.environ.get("LINE_CHANNEL_SECRET", "").strip()
    )
    channel_token = (
        token.strip()
        or _secret_get("LINE_CHANNEL_ACCESS_TOKEN", "line_channel_access_token")
        or _os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )
    uid = (
        user_id.strip()
        or _secret_get("LINE_USER_ID", "line_user_id")
        or _os.environ.get("LINE_USER_ID", "").strip()
    )
    return {
        "channel_secret": channel_secret,
        "channel_access_token": channel_token,
        "user_id": uid,
    }


def render_onboarding_checklist() -> None:
    """店舗オーナー向け導入チェックリスト（インタラクティブ）。"""
    st.subheader("導入チェックリスト")
    st.caption(
        "御店の LINE 公式で希望休→組表まで繋ぐための実務チェック。"
        " 開発者個人 LINE は不要です。所要の目安は合計 約5〜30分"
        "（公式が既にある店は短め、新規作成からだと長め）。"
    )

    steps = [
        {
            "key": "ob_line_oa",
            "label": "① LINE 公式アカウントを用意する",
            "mins": "5–10分",
            "hint": "Official Account Manager で店舗公式を作成／既存を使用",
        },
        {
            "key": "ob_messaging",
            "label": "② Messaging API チャネル（secret / 長期トークン）",
            "mins": "5分",
            "hint": "LINE Developers。応答・あいさつメッセージはオフ推奨",
        },
        {
            "key": "ob_webhook",
            "label": "③ Webhook URL を設定・検証する",
            "mins": "5–10分",
            "hint": "HTTPS の …/webhook（PoC は ngrok 可）。Webhook 利用オン",
        },
        {
            "key": "ob_env",
            "label": "④ 環境変数（.env）を入れる",
            "mins": "3–5分",
            "hint": "LINE_CHANNEL_SECRET / ACCESS_TOKEN。デモのみなら省略可",
        },
        {
            "key": "ob_store",
            "label": "⑤ Streamlit「店舗向け」で店舗を作成",
            "mins": "2–3分",
            "hint": "招待コードを控える（例: MINA01）",
        },
        {
            "key": "ob_invite",
            "label": "⑥ スタッフを友だち追加＋「登録 店舗コード」で招待",
            "mins": "3–5分",
            "hint": "メンバー（マスク）が増えたら OK",
        },
        {
            "key": "ob_test",
            "label": "⑦ 試験: 登録 / 希望休 / シフト見せて",
            "mins": "5分",
            "hint": "必須3発話が返れば接続成功",
        },
        {
            "key": "ob_golive",
            "label": "⑧ 本番前チェック・お試し範囲の合意",
            "mins": "3–5分",
            "hint": "go_live.md の課金前チェックを確認",
        },
    ]

    done = 0
    for step in steps:
        if step["key"] not in st.session_state:
            st.session_state[step["key"]] = False
        checked = st.checkbox(
            f"{step['label']}  （目安 {step['mins']}）",
            key=step["key"],
            help=step["hint"],
        )
        st.caption(step["hint"])
        if checked:
            done += 1

    total = len(steps)
    pct = int(round(100.0 * done / total)) if total else 0
    st.progress(pct / 100.0)
    st.markdown(
        f"**進捗: {done}/{total}（{pct}%）**　"
        f"合計目安 約5〜30分（公式の有無で幅あり）"
    )

    if st.button("すべて外す", key="ob_reset_btn"):
        for step in steps:
            st.session_state[step["key"]] = False
        st.rerun()

    st.markdown(
        """**ドキュメント（リポジトリ内）**
- 詳細手順: `examples/community_mina/line_bridge/ONBOARDING_CHECKLIST.md`
- 今週お試し課金: `examples/community_mina/pitch/go_live.md`
- 技術メモ: `examples/community_mina/line_bridge/README.md`"""
    )

    with st.expander("チェックリスト本文をこの画面で開く", expanded=False):
        if ONBOARDING_MD.exists():
            st.markdown(ONBOARDING_MD.read_text(encoding="utf-8"))
        else:
            st.warning("ONBOARDING_CHECKLIST.md が見つかりません。")
    with st.expander("go_live（今週お試し課金）を開く", expanded=False):
        if GO_LIVE_MD.exists():
            st.markdown(GO_LIVE_MD.read_text(encoding="utf-8"))
        else:
            st.warning("go_live.md が見つかりません。")
    st.markdown(
        """
<div class="product-note">
  <b>販売時の前提</b>：お客様の LINE 公式が必要。開発者個人 LINE は不要。
  資格情報未設定時はデモ／プレビューのみ（ライブ送信を主張しない）。
</div>
""",
        unsafe_allow_html=True,
    )


def render_store_section(result: dict | None) -> None:
    """店舗オーナー／スタッフ向けオンボーディング（マルチテナント）。"""
    st.subheader("店舗向け（マルチテナント登録）")
    st.caption(
        "販売先は店舗オーナー／スタッフです。お客様の LINE 公式を使い、"
        "店長が招待コードを発行 → スタッフが「登録 店舗コード」で紐付けます。"
        "開発者個人 LINE は不要です。"
    )

    if line_stores is None or line_msgs is None or line_notify is None:
        st.warning(
            "line_bridge（stores）を読み込めませんでした: "
            + str(_LINE_IMPORT_ERROR)
        )
        return

    # デモ店舗を用意
    try:
        line_stores.ensure_demo_store()
    except Exception as exc:  # noqa: BLE001
        st.caption(f"デモ店舗の準備をスキップ: {exc}")

    with st.expander("店舗を作成", expanded=True):
        c1, c2 = st.columns([2, 1])
        with c1:
            new_name = st.text_input(
                "店舗名",
                value="サンプルカフェ",
                key="store_new_name",
            )
        with c2:
            custom_code = st.text_input(
                "招待コード（空欄で自動）",
                value="",
                key="store_new_code",
                help="英数字 3〜16 文字。空なら自動生成。",
            )
        if st.button("店舗を作成", type="primary", key="store_create_btn"):
            try:
                rec = line_stores.create_store(
                    new_name,
                    invite_code=custom_code.strip() or None,
                )
                st.session_state["store_last_created"] = rec
                st.success(
                    f"作成しました: {rec['store_name']} ／ コード {rec['invite_code']}"
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"作成エラー: {exc}")

    stores = line_stores.list_stores()
    if not stores:
        st.info(
            "まだ店舗がありません。「店舗を作成」するか、デモコード DEMO01 を使えます。"
        )
        return

    names = {
        f"{s['store_name']}（{s['invite_code']}）": s["store_id"] for s in stores
    }
    pick = st.selectbox("操作する店舗", list(names.keys()), key="store_pick")
    sid = names[pick]
    store = line_stores.get_store(sid) or next(
        s for s in stores if s["store_id"] == sid
    )

    invite_url = line_stores.invite_url_placeholder(store["invite_code"])
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("招待コード", store["invite_code"])
    with m2:
        st.metric("登録メンバー", len(store.get("line_user_ids") or []))
    with m3:
        st.metric("store_id", store["store_id"][:12] + "…")

    st.markdown("**招待リンク（QR プレースホルダ）**")
    st.code(invite_url, language=None)
    st.caption(
        "本番では LINE 公式の友だち追加 URL に差し替え、スタッフへコードを案内してください。"
        " スタッフは友だち追加後、チャットで「登録 "
        + store["invite_code"]
        + "」と送ります。"
    )

    st.markdown("**登録メンバー（userId マスク）**")
    rows = line_stores.member_rows_masked(store)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption(
            "まだメンバーがいません。デモでは webhook で「登録 "
            + store["invite_code"]
            + "」を送ると追加されます。"
        )

    st.markdown("**シフト生成 → メンバーへブロードキャスト（プレビュー）**")
    use_flex = st.checkbox("Flex Message で送る", value=True, key="store_bc_flex")
    force_demo = st.checkbox(
        "デモモードでプレビューのみ（実送信しない）",
        value=True,
        key="store_bc_demo",
        help="営業デモではオン推奨。トークンが無くてもプレビューできます。",
    )
    if st.button(
        "組表して全員へブロードキャストプレビュー",
        key="store_bc_btn",
        use_container_width=True,
    ):
        from solver_bridge import run_classical_week

        if result is not None:
            sc = dict(result["scenario"])
            classical = result["classical"]
            prefs = (store.get("preferences") or {}).get("preferred_offs")
            if prefs:
                sc = dict(sc)
                sc["preferred_offs"] = prefs
                classical = run_classical_week(sc)
            line_result = {"scenario": sc, "classical": classical}
        else:
            sc = line_msgs.scenario_for_store(store)
            classical = run_classical_week(sc)
            line_result = {"scenario": sc, "classical": classical}

        extra = f"【{store['store_name']}】今週のシフトたたき台です（PoC）。"
        if force_demo:
            import os as _os

            _os.environ["LINE_DEMO_MODE"] = "true"
        out = line_notify.broadcast_to_store(
            store,
            result=line_result,
            use_flex=use_flex,
            extra_text=extra,
            dry_run=True if force_demo else None,
        )
        st.session_state["store_bc_preview"] = {
            "detail": out.get("detail"),
            "mode": out.get("mode"),
            "sent_count": out.get("sent_count"),
            "targets_masked": [
                line_stores.mask_user_id(u) for u in (out.get("targets") or [])
            ],
            "text": line_msgs.build_shift_text(line_result),
            "messages": out.get("messages"),
        }

    bc = st.session_state.get("store_bc_preview")
    if bc:
        st.info(bc["detail"] + f" ／ mode={bc['mode']} ／ sent={bc['sent_count']}")
        if bc["targets_masked"]:
            st.caption("宛先（マスク）: " + ", ".join(bc["targets_masked"]))
        st.code(bc["text"], language=None)
        with st.expander("ブロードキャスト messages JSON"):
            import json as _json

            st.code(
                _json.dumps(bc["messages"], ensure_ascii=False, indent=2),
                language="json",
            )

    st.markdown(
        """
<div class="product-note">
  <b>売り文句</b>：御店の LINE 公式にスタッフが友だち追加 →「登録 店舗コード」→
  希望休／シフトが店舗単位で動く。開発者の個人 LINE は使いません。
</div>
""",
        unsafe_allow_html=True,
    )



def render_line_section(result: dict | None) -> None:
    """営業デモ用 LINE 連携セクション。実送信は資格情報があるときのみ。"""
    st.subheader("LINE連携")
    st.caption(
        "希望休の受付 → 自動組表 → 通知、の流れを見せる PoC 層です。"
        " 資格情報未設定時はデモモード（プレビューのみ・実 LINE 非送信）。"
    )

    if line_msgs is None or line_notify is None:
        st.warning(
            "line_bridge を読み込めませんでした: "
            + str(_LINE_IMPORT_ERROR)
        )
        st.caption(f"想定パス: `{LINE_BRIDGE}`")
        return

    with st.expander("LINE 資格情報（任意・st.secrets / 環境変数でも可）", expanded=False):
        st.markdown(
            "Channel secret / アクセストークン / userId は "
            "[LINE Developers](https://developers.line.biz/console/) で取得。"
            " 手順は `examples/community_mina/line_bridge/README.md`。"
        )
        col_s, col_t = st.columns(2)
        with col_s:
            ui_secret = st.text_input(
                "Channel secret",
                value="",
                type="password",
                key="line_ui_secret",
                help="空なら st.secrets または環境変数 LINE_CHANNEL_SECRET",
            )
            ui_user = st.text_input(
                "プッシュ先 userId",
                value="",
                key="line_ui_user",
                help="空なら st.secrets / LINE_USER_ID",
            )
        with col_t:
            ui_token = st.text_input(
                "Channel access token",
                value="",
                type="password",
                key="line_ui_token",
                help="空なら st.secrets または環境変数 LINE_CHANNEL_ACCESS_TOKEN",
            )
            force_demo = st.checkbox(
                "強制デモモード（実送信しない）",
                value=True,
                key="line_force_demo",
                help="営業デモではオン推奨。資格情報があっても API を呼びません。",
            )

    creds = _line_creds_from_ui(
        st.session_state.get("line_ui_secret", ""),
        st.session_state.get("line_ui_token", ""),
        st.session_state.get("line_ui_user", ""),
    )
    force_demo = bool(st.session_state.get("line_force_demo", True))
    status = line_msgs.connection_status(creds, force_demo=force_demo)

    if status == "接続済" and not force_demo:
        badge = '<span class="badge-ok">接続済</span>'
        hint = "token + secret あり。テスト送信で実 API を呼べます（自己責任）。"
    elif status == "デモモード":
        badge = '<span class="badge-soft">デモモード</span>'
        hint = "プレビューとログのみ。実 LINE には送りません。"
    else:
        badge = '<span class="badge-ng">未設定</span>'
        hint = "資格情報なし。メッセージ生成プレビューのみ利用できます。"

    st.markdown(
        f"**ステータス:** {badge}　"
        f'<span style="color:#64748b;font-size:0.88rem;">{hint}</span>',
        unsafe_allow_html=True,
    )

    use_flex = st.radio(
        "メッセージ形式",
        ["Flex Message（表）", "テキストのみ"],
        horizontal=True,
        key="line_msg_format",
    )
    use_flex_flag = use_flex.startswith("Flex")

    gen = st.button(
        "LINE向けメッセージを生成",
        use_container_width=True,
        key="line_generate",
    )

    if gen:
        if result is None:
            # サンプルで組む
            from solver_bridge import run_classical_week

            sc = load_sample_scenario()
            classical = run_classical_week(sc)
            line_result = {"scenario": sc, "classical": classical}
            st.info("シフト結果が無いため、サンプル店シナリオでメッセージを生成しました。")
        else:
            line_result = {
                "scenario": result["scenario"],
                "classical": result["classical"],
            }
        messages = line_notify.build_messages(
            line_result,
            use_flex=use_flex_flag,
            extra_text="今週のシフトたたき台です（PoC）。",
        )
        st.session_state["line_preview"] = {
            "messages": messages,
            "text": line_msgs.build_shift_text(line_result),
            "result": line_result,
            "use_flex": use_flex_flag,
        }

    preview = st.session_state.get("line_preview")
    if preview:
        st.markdown("**送信プレビュー（実送信前の中身）**")
        st.code(preview["text"], language=None)
        with st.expander("LINE Messaging API 用 JSON（messages）"):
            import json as _json

            st.code(
                _json.dumps(preview["messages"], ensure_ascii=False, indent=2),
                language="json",
            )
        st.caption(
            "上記は生成結果です。ステータスが「接続済」かつ強制デモがオフで、"
            "userId があるときだけ下のテスト送信が実 API を呼びます。"
        )

        can_live = (
            status == "接続済"
            and not force_demo
            and bool(creds.get("user_id"))
            and bool(creds.get("channel_access_token"))
        )
        send = st.button(
            "テスト送信",
            disabled=not can_live,
            key="line_test_send",
            help="接続済かつ userId 設定時のみ有効。デモ時は無効。",
        )
        if send and can_live:
            # env に載せて notify に渡す
            line_msgs.inject_credentials(
                channel_secret=creds["channel_secret"],
                channel_access_token=creds["channel_access_token"],
                user_id=creds["user_id"],
            )
            out = line_notify.push_messages(
                user_id=creds["user_id"],
                messages=preview["messages"],
                dry_run=False,
            )
            if out.get("sent"):
                st.success(out.get("detail", "送信しました。"))
            else:
                st.warning(
                    out.get("detail", "送信できませんでした。")
                    + f" mode={out.get('mode')}"
                )
        elif not can_live:
            st.caption(
                "テスト送信は無効です（未設定／デモモード、または userId 不足）。"
                " プレビューだけで営業説明できます。"
            )
    else:
        st.info(
            "「LINE向けメッセージを生成」を押すと、週次表のテキスト／Flex プレビューが出ます。"
        )

    st.markdown(
        """
<div class="product-note">
  <b>売り文句の核</b>：御店の LINE 公式で希望休 → 自動組表 → 通知。
  スタッフは「登録 店舗コード」で紐付け（マルチテナント）。
  開発者個人 LINE は不要。Webhook は
  <code>examples/community_mina/line_bridge/</code>。
  ライブ動作は<strong>お客様側</strong>の Channel secret / token が必要です。
</div>
""",
        unsafe_allow_html=True,
    )



def execute_demo(scenario: dict, shots: int, seed: int, run_quantum: bool) -> None:
    with st.spinner(
        "古典ソルバで週次シフトを作成中…"
        + ("／ OpenQARP QAOA 比較も実行" if run_quantum else "")
    ):
        try:
            result = run_full_demo(
                scenario,
                shots=int(shots),
                seed=int(seed),
                run_quantum=bool(run_quantum),
            )
            st.session_state["last_result"] = result
        except Exception as exc:  # noqa: BLE001
            st.error(f"実行エラー: {exc}")
            st.session_state["last_result"] = None


# ---- ヒーロー ----
st.markdown(
    """
<div class="hero-wrap">
  <div class="hero-kicker">SALES DEMO ／ OpenQARP COMMUNITY PoC</div>
  <div class="hero-title">OpenQARPで試作した店舗シフトPoC</div>
  <p class="hero-sub">
    毎週のシフトたたき台をボタン1つで。希望休の一致も見える営業デモ。<br/>
    基盤はオープンソースの量子アプリケーション開発キット <b>OpenQARP</b>。
  </p>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="value-grid">
  <div class="value-card">
    <b>⏱ たたき台がすぐ出る</b>
    <span>Excel で1人ずつ埋める前に、週次の出勤／休み案を画面で確認できます。</span>
  </div>
  <div class="value-card">
    <b>✓ 希望休の漏れが見える</b>
    <span>「通った／通らなかった」をバッジで表示。店長の確認コストを下げます。</span>
  </div>
  <div class="value-card">
    <b>🔬 技術の載せ方が見える</b>
    <span>本体は古典。OpenQARP の QAOA 部品は同じ画面で比較デモとして並べます。</span>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# ---- Before / After ----
st.markdown(
    """
<div class="ba-grid">
  <div class="ba-card ba-before">
    <h4>手作業だと迷いやすい点</h4>
    <ul>
      <li>希望休がぶつかると、誰を休ませるかで毎回悩む</li>
      <li>日曜など忙しい日の人数確保を後から気づく</li>
      <li>「たたき台」が無いので最初の1行から手で埋める</li>
    </ul>
  </div>
  <div class="ba-card ba-after">
    <h4>この PoC だとこう出る</h4>
    <ul>
      <li>ボタン1つで週次表＋希望休の通否バッジ</li>
      <li>注目日の出勤／休み案をすぐ指差しできる</li>
      <li>古典本体＋ OpenQARP 比較を同じ画面で見せられる</li>
    </ul>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="disclaimer-box">
  <b>製品の中心は古典です。</b>
  週次シフト最適化は<strong>古典ソルバ</strong>（貪欲＋局所改善）が本体です。
  OpenQARP の量子アルゴリズム部品（QAOA）は、注目日の希望休コンフリクトを縮小した
  Max-Cut への<strong>比較・将来拡張デモ</strong>です。
  量子が古典に勝つとは主張しません。既存シフト SaaS の置き換えでもありません。
</div>
""",
    unsafe_allow_html=True,
)

# ---- サイドバー（詳細設定のみ） ----
with st.sidebar:
    st.header("詳細設定")
    st.caption("ワンクリックデモでは触らなくてOK。")
    shots = st.slider("QAOA ショット数", 200, 2000, 1000, 100)
    seed = st.number_input("乱数シード", value=1234, step=1)
    run_quantum = st.checkbox(
        "量子比較を実行 (OpenQARP / QAOA)",
        value=True,
        help="オフにすると古典のみ。qarp が無い場合は自動でスキップされます。",
    )
    qarp_ok = qarp_available()
    if qarp_ok:
        st.markdown(
            '<span class="badge-ok">OpenQARP 利用可</span>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<span class="badge-ng">OpenQARP 未検出</span> '
            '<span class="badge-soft">古典のみ</span>',
            unsafe_allow_html=True,
        )
    st.divider()
    st.markdown("**ブランド**")
    st.caption("OpenQARP — オープンソースの量子アプリケーション開発キット")
    st.caption("製品呼び方: OpenQARPで試作した店舗シフトPoC")

# ---- ワンクリックデモ ----
st.markdown(
    """
<div class="oneclick-wrap">
  <b style="font-size:1.02rem;color:#0f172a;">ワンクリック・デモ</b>
  <p class="hint">
    サンプル店（4名・希望休入り）を読み込み、フォーム操作なしで週次たたき台まで実行します。
  </p>
</div>
""",
    unsafe_allow_html=True,
)

if "last_result" not in st.session_state:
    st.session_state["last_result"] = None

demo_clicked = st.button(
    "サンプル店で今すぐ組む",
    type="primary",
    use_container_width=True,
    key="oneclick_demo",
)

if demo_clicked:
    execute_demo(load_sample_scenario(), shots, seed, run_quantum)

# ---- 詳細入力（折りたたみ） ----
default = default_scenario()
with st.expander("詳細設定・シナリオ編集（任意）", expanded=False):
    input_mode = st.radio(
        "入力方法",
        ["フォーム", "JSON 読込"],
        horizontal=True,
        key="adv_input_mode",
    )
    advanced_scenario: dict
    if input_mode == "JSON 読込":
        uploaded = st.file_uploader("シナリオ JSON", type=["json"])
        sample_btn = st.button("サンプル JSON を読み込む", key="adv_sample_json")
        raw = None
        if uploaded is not None:
            raw = uploaded.read().decode("utf-8")
        elif sample_btn and SCENARIO_JSON.exists():
            raw = SCENARIO_JSON.read_text(encoding="utf-8")
            st.code(raw, language="json")
        if raw:
            try:
                advanced_scenario = load_scenario_json(raw)
                st.success("シナリオを読み込みました。")
            except Exception as exc:  # noqa: BLE001
                st.error(f"JSON エラー: {exc}")
                advanced_scenario = default_scenario()
        else:
            advanced_scenario = default_scenario()
            st.info("JSON をアップロードするか「サンプル」を押してください（未指定時は既定）。")
    else:
        col_a, col_b = st.columns(2)
        with col_a:
            workers_text = st.text_input(
                "スタッフ名（カンマ区切り）",
                value=", ".join(default["workers"]),
            )
            min_staff = st.number_input(
                "1日の最低人数",
                min_value=1,
                max_value=10,
                value=int(default["min_staff_per_day"]),
            )
            max_consec = st.number_input(
                "連続勤務上限（日）",
                min_value=1,
                max_value=7,
                value=int(default["max_consecutive_days"]),
            )
        with col_b:
            focus_day = st.selectbox(
                "量子比較の注目日",
                default["days"],
                index=default["days"].index(default["qaoa_focus_day"]),
            )
            need_on = st.number_input(
                "注目日の必要出勤人数",
                min_value=1,
                max_value=10,
                value=int(default["qaoa_needed_on_focus"]),
            )
            st.caption("希望休は下の表で編集（スタッフ × 曜日）")

        pref_default = []
        for w, offs in default.get("preferred_offs", {}).items():
            for day in offs:
                pref_default.append({"スタッフ": w, "希望休": day})
        pref_df = st.data_editor(
            pd.DataFrame(pref_default or [{"スタッフ": "A", "希望休": "日"}]),
            num_rows="dynamic",
            width="stretch",
            key="pref_editor",
        )
        advanced_scenario = build_scenario_from_ui(
            workers_text,
            int(min_staff),
            int(max_consec),
            focus_day,
            int(need_on),
            pref_df.to_dict("records"),
        )

    custom_run = st.button("この条件でシフトを組む", key="custom_run")
    if custom_run:
        execute_demo(advanced_scenario, shots, seed, run_quantum)

result = st.session_state["last_result"]

if result is None:
    st.info(
        "上の「サンプル店で今すぐ組む」を押すと、週次表・提案サマリー・比較パネルが表示されます。"
    )
    st.markdown(
        """
<div class="cta-box">
  <b>次の一歩（営業用）</b><br/>
  お試し: 画面共有でシナリオ合わせ 0〜数万円／回（仮） →
  店舗カスタム: 月額数万円〜（仮） →
  正式見積は別途。<br/>
  <span style="color:#64748b;font-size:0.88rem;">
  Credit: Powered by OpenQARP（オープンソースの量子アプリケーション開発キット）
  </span>
</div>
""",
        unsafe_allow_html=True,
    )
else:
    classical = result["classical"]
    exact = result["exact"]
    quantum = result["quantum"]
    sc = result["scenario"]

    pref_ok = sum(1 for h in classical["pref_hits"] if h["granted"])
    pref_all = len(classical["pref_hits"])
    agree_exact = bool(quantum.get("agree_exact")) if quantum.get("available") else None

    st.subheader("提案サマリー")
    render_proposal_card(result)

    q_runtime = (
        f"{quantum['qaoa_seconds']:.3f} s"
        if quantum.get("available")
        else "—"
    )
    if agree_exact is True:
        agree_html = _badge(True, "一致", "不一致")
    elif agree_exact is False:
        agree_html = _badge(False, "一致", "不一致")
    else:
        agree_html = '<span class="badge-soft">量子スキップ</span>'

    st.markdown(
        f"""
<div class="metric-strip">
  <div class="metric-card">
    <div class="label">スコア（高いほど良い）</div>
    <div class="value">{classical['score']:.1f}</div>
    <div class="hint">古典ソルバ（本体）</div>
  </div>
  <div class="metric-card">
    <div class="label">希望休充足</div>
    <div class="value">{pref_ok}/{pref_all if pref_all else 0}</div>
    <div class="hint">通った希望 / 指定数</div>
  </div>
  <div class="metric-card">
    <div class="label">ランタイム</div>
    <div class="value">{classical['seconds']*1000:.2f}<span style="font-size:0.85rem;"> ms</span></div>
    <div class="hint">古典週次 ／ QAOA {q_runtime}</div>
  </div>
  <div class="metric-card">
    <div class="label">一致バッジ（QAOA↔厳密）</div>
    <div class="value" style="font-size:1.15rem;padding-top:0.25rem;">{agree_html}</div>
    <div class="hint">勝ち主張ではなく並び確認</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown(
        """
<div class="product-note">
  <b>本デモの製品部分</b>は下の週次シフト表（古典）です。
  その下の「古典 vs 量子」は OpenQARP 部品の比較パネルです。
</div>
""",
        unsafe_allow_html=True,
    )

    st.subheader("週次シフト表（古典ソルバ）")
    st.caption("✓希望 = 希望休どおり休み ／ ⚠希望 = 希望を出勤にせざるを得なかった日")
    df = schedule_to_dataframe(classical["schedule"], sc)
    st.dataframe(style_schedule_df(df), width="stretch", hide_index=True)

    st.markdown("**希望休チェック**")
    if classical["pref_hits"]:
        cols = st.columns(min(5, len(classical["pref_hits"])))
        for i, hit in enumerate(classical["pref_hits"]):
            with cols[i % len(cols)]:
                st.markdown(
                    f"{hit['worker']}・{hit['day']} {_badge(hit['granted'])}",
                    unsafe_allow_html=True,
                )
    else:
        st.caption("希望休の指定はありません。")

    st.divider()

    st.subheader(f"古典 vs 量子（注目日: {sc['qaoa_focus_day']}）— 比較デモ")
    st.caption(
        "量子側は OpenQARP の QAOA で、注目日の希望休コンフリクトを "
        "縮小 Max-Cut に落とした比較デモです。週全体のシフト本体ではありません。"
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            f"""
<div class="compare-card">
  <h5>古典・週次から抜粋</h5>
  <div>出勤: <b>{', '.join(classical['focus_on']) or '—'}</b></div>
  <div>休み: <b>{', '.join(classical['focus_off']) or '—'}</b></div>
  <div style="margin-top:0.55rem;color:#64748b;font-size:0.85rem;">
    runtime {classical['seconds']*1000:.2f} ms（週全体）
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""
<div class="compare-card">
  <h5>古典・注目日の厳密解</h5>
  <div>出勤: <b>{', '.join(exact['on'])}</b></div>
  <div>休み: <b>{', '.join(exact['off'])}</b></div>
  <div style="margin-top:0.55rem;color:#64748b;font-size:0.85rem;">
    runtime {exact['seconds']*1000:.3f} ms ／ prefスコア {exact['pref_score']:.1f}
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
    with c3:
        if quantum.get("available"):
            st.markdown(
                f"""
<div class="compare-card">
  <h5>OpenQARP QAOA（縮小）</h5>
  <div>出勤: <b>{', '.join(quantum['qaoa_on'])}</b></div>
  <div>休み: <b>{', '.join(quantum['qaoa_off'])}</b></div>
  <div style="margin-top:0.55rem;color:#64748b;font-size:0.85rem;">
    runtime {quantum['qaoa_seconds']:.3f} s ／ cut={quantum['qaoa_best_cut']:g}
    ／ prefスコア {quantum['pref_score']:.1f}
  </div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            st.warning(quantum.get("error", "量子比較をスキップしました。"))

    if quantum.get("available"):
        a1, a2, a3 = st.columns(3)
        with a1:
            st.markdown(
                "QAOA ↔ 古典厳密　" + _badge(quantum["agree_exact"], "一致", "不一致"),
                unsafe_allow_html=True,
            )
        with a2:
            st.markdown(
                "QAOA ↔ 週次古典　" + _badge(result["agree_week"], "一致", "不一致"),
                unsafe_allow_html=True,
            )
        with a3:
            st.markdown(
                "カット最適内　" + _badge(quantum["cut_agree"], "含む", "含まない"),
                unsafe_allow_html=True,
            )

        with st.expander("QAOA サンプル頻度・パラメータ（技術詳細）"):
            freq_rows = [
                {
                    "bits": bits,
                    "回数": count,
                    "割合": f"{count / quantum['shots']:.3f}",
                }
                for bits, count in quantum["frequencies"]
            ]
            st.dataframe(pd.DataFrame(freq_rows), hide_index=True, width="stretch")
            st.caption(
                "parameters: "
                + ", ".join(f"{v:.4f}" for v in quantum["parameters"])
            )
            st.caption(
                f"classical Max-Cut best={quantum['cut_exact']:g} "
                f"({quantum['cut_seconds']*1000:.3f} ms)"
            )

    st.markdown(
        """
<div class="disclaimer-box">
  一致しても「量子が優れている」意味にはなりません。
  大規模シフト・労働法規・公平性の厳密モデルは専用の古典ソルバ／既存アプリ向けです。
  本 PoC は OpenQARP の部品を店舗オペレーションにどう載せられるかを見せる試作です。
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown(
        """
<div class="cta-box">
  <b>次の一歩</b>：お試し PoC（画面合わせ 0〜数万円／回・仮）→
  店舗カスタム（月額数万円〜・仮）→ 連携は個別見積。<br/>
  今日は感触確認で OK。正式見積は別途。<br/>
  <span style="color:#64748b;font-size:0.88rem;">
  Credit: Powered by <b>OpenQARP</b> — オープンソースの量子アプリケーション開発キット
  </span>
</div>
""",
        unsafe_allow_html=True,
    )

# ---- 導入チェックリスト ＋ 店舗向け ＋ LINE 連携 ----
st.divider()
render_onboarding_checklist()
st.divider()
render_store_section(result)
st.divider()
render_line_section(result)

# ---- ワンページピッチ ----
st.divider()
st.subheader("ワンページ・ピッチ")
if PITCH_MD.exists():
    st.markdown(PITCH_MD.read_text(encoding="utf-8"))
else:
    st.warning("PITCH.md が見つかりません。")

st.caption(
    "© OpenQARP コミュニティ試作 ／ examples/community_mina/pitch ／ "
    "量子優位性は主張しません ／ Powered by OpenQARP"
)
