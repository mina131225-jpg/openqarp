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
SCENARIO_JSON = HERE.parent / "shift_scenario_tiny.json"

st.set_page_config(
    page_title="OpenQARPで試作した店舗シフトPoC",
    page_icon="🗓️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- スタイル ----
st.markdown(
    """
<style>
  .hero-wrap {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 55%, #1d4ed8 100%);
    border-radius: 1rem; padding: 1.35rem 1.5rem 1.2rem; margin-bottom: 1rem;
    color: #f8fafc; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
  }
  .hero-kicker {
    font-size: 0.78rem; letter-spacing: 0.06em; text-transform: uppercase;
    color: #93c5fd; font-weight: 600; margin-bottom: 0.35rem;
  }
  .hero-title {
    font-size: 1.85rem; font-weight: 800; line-height: 1.25; margin: 0 0 0.45rem;
  }
  .hero-sub { color: #cbd5e1; font-size: 0.98rem; margin: 0; }
  .value-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.75rem;
    margin: 0.85rem 0 0.35rem;
  }
  .value-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 0.75rem;
    padding: 0.85rem 1rem; box-shadow: 0 1px 2px rgba(15,23,42,0.04);
  }
  .value-card b { display: block; color: #0f172a; font-size: 0.98rem; margin-bottom: 0.25rem; }
  .value-card span { color: #475569; font-size: 0.88rem; line-height: 1.45; }
  .badge-ok {
    display: inline-block; background: #d1fae5; color: #065f46;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 700; font-size: 0.85rem;
  }
  .badge-ng {
    display: inline-block; background: #fee2e2; color: #991b1b;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 700; font-size: 0.85rem;
  }
  .badge-info {
    display: inline-block; background: #e0e7ff; color: #3730a3;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 700; font-size: 0.85rem;
  }
  .badge-soft {
    display: inline-block; background: #f1f5f9; color: #334155;
    padding: 0.18rem 0.65rem; border-radius: 999px; font-weight: 600; font-size: 0.82rem;
  }
  .disclaimer-box {
    background: #fffbeb; border-left: 4px solid #f59e0b;
    padding: 0.85rem 1.05rem; border-radius: 0.45rem; margin: 0.35rem 0 1rem;
    font-size: 0.92rem; color: #78350f;
  }
  .product-note {
    background: #f0fdf4; border-left: 4px solid #22c55e;
    padding: 0.7rem 1rem; border-radius: 0.45rem; margin: 0.5rem 0 1rem;
    font-size: 0.9rem; color: #14532d;
  }
  .metric-strip {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem;
    margin: 0.5rem 0 1rem;
  }
  .metric-card {
    background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 0.75rem; padding: 0.85rem 1rem;
  }
  .metric-card .label { color: #64748b; font-size: 0.82rem; margin-bottom: 0.2rem; }
  .metric-card .value { color: #0f172a; font-size: 1.45rem; font-weight: 800; line-height: 1.2; }
  .metric-card .hint { color: #94a3b8; font-size: 0.78rem; margin-top: 0.2rem; }
  .compare-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 0.75rem;
    padding: 0.9rem 1rem; min-height: 9.5rem;
  }
  .compare-card h5 { margin: 0 0 0.55rem; font-size: 0.98rem; color: #1e293b; }
  .cta-box {
    background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 0.75rem;
    padding: 0.95rem 1.1rem; margin: 0.75rem 0 0.25rem;
  }
  div[data-testid="stDataFrame"] { font-size: 0.95rem; }
  @media (max-width: 900px) {
    .value-grid, .metric-strip { grid-template-columns: 1fr; }
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
        row = {"スタッフ": w}
        for d_idx, day in enumerate(days):
            on = schedule[w][d_idx]
            cell = "出勤" if on else "休み"
            if day in prefs.get(w, []):
                cell = ("出勤⚠希望" if on else "休み✓希望")
            row[day] = cell
        row["出勤日数"] = int(sum(schedule[w]))
        rows.append(row)
    # 人数行 — Arrow 互換のため出勤日数は "-"（空文字にしない）
    daily = {"スタッフ": "（人数）"}
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

# ---- サイドバー ----
with st.sidebar:
    st.header("設定")
    st.caption("スタッフ名・制約を編集するか、JSON を読み込みます。")

    default = default_scenario()
    input_mode = st.radio(
        "入力方法",
        ["フォーム", "JSON 読込"],
        horizontal=True,
    )

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


# ---- シナリオ入力 ----
scenario: dict
if input_mode == "JSON 読込":
    uploaded = st.file_uploader("シナリオ JSON", type=["json"])
    sample_btn = st.button("サンプル JSON を読み込む")
    raw = None
    if uploaded is not None:
        raw = uploaded.read().decode("utf-8")
    elif sample_btn and SCENARIO_JSON.exists():
        raw = SCENARIO_JSON.read_text(encoding="utf-8")
        st.code(raw, language="json")
    if raw:
        try:
            scenario = load_scenario_json(raw)
            st.success("シナリオを読み込みました。")
        except Exception as exc:
            st.error(f"JSON エラー: {exc}")
            scenario = default_scenario()
    else:
        scenario = default_scenario()
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
    scenario = build_scenario_from_ui(
        workers_text,
        int(min_staff),
        int(max_consec),
        focus_day,
        int(need_on),
        pref_df.to_dict("records"),
    )

# ---- 実行ボタン ----
run = st.button("シフトを組む", type="primary")

if "last_result" not in st.session_state:
    st.session_state["last_result"] = None

if run:
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
        except Exception as exc:
            st.error(f"実行エラー: {exc}")
            st.session_state["last_result"] = None

result = st.session_state["last_result"]

if result is None:
    st.markdown("---")
    st.info("左で条件を確認し、「シフトを組む」を押すと週次表と比較パネルが表示されます。")
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
    agree_week = bool(result.get("agree_week")) if quantum.get("available") else None

    # ---- メトリクス（HTML カード） ----
    q_runtime = (
        f"{quantum['qaoa_seconds']:.3f} s"
        if quantum.get("available")
        else "—"
    )
    agree_html = (
        _badge(True, "一致", "不一致")
        if agree_exact is True
        else (
            _badge(False, "一致", "不一致")
            if agree_exact is False
            else '<span class="badge-soft">量子スキップ</span>'
        )
    )
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

    # ---- 比較パネル ----
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
