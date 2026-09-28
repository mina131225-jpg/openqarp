#!/usr/bin/env python3
"""OpenQARP で試作した店舗シフト PoC — 営業向けデモ UI (Streamlit)。

本体のシフト最適化は古典ヒューリスティック。
比較・将来拡張に OpenQARP の量子アルゴリズム部品 (QAOA) を使う。
量子が古典に勝つとは主張しない。既存シフト SaaS の置き換えでもない。
"""

from __future__ import annotations

import json
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
    page_title="店舗シフト PoC | OpenQARP",
    page_icon="🗓️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- スタイル ----
st.markdown(
    """
<style>
  .main-title { font-size: 1.85rem; font-weight: 700; margin-bottom: 0.2rem; }
  .sub-brand { color: #4b5563; font-size: 0.95rem; margin-bottom: 1rem; }
  .badge-ok {
    display: inline-block; background: #d1fae5; color: #065f46;
    padding: 0.15rem 0.55rem; border-radius: 999px; font-weight: 600; font-size: 0.85rem;
  }
  .badge-ng {
    display: inline-block; background: #fee2e2; color: #991b1b;
    padding: 0.15rem 0.55rem; border-radius: 999px; font-weight: 600; font-size: 0.85rem;
  }
  .badge-info {
    display: inline-block; background: #e0e7ff; color: #3730a3;
    padding: 0.15rem 0.55rem; border-radius: 999px; font-weight: 600; font-size: 0.85rem;
  }
  .disclaimer-box {
    background: #fffbeb; border-left: 4px solid #f59e0b;
    padding: 0.75rem 1rem; border-radius: 0.35rem; margin: 0.5rem 0 1rem;
    font-size: 0.9rem;
  }
  .metric-card {
    background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 0.5rem; padding: 0.75rem 1rem;
  }
  div[data-testid="stDataFrame"] { font-size: 0.95rem; }
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
        row["出勤日数"] = sum(schedule[w])
        rows.append(row)
    # 人数行
    daily = {"スタッフ": "（人数）"}
    for d_idx, day in enumerate(days):
        daily[day] = str(sum(1 for w in workers if schedule[w][d_idx]))
    daily["出勤日数"] = ""
    rows.append(daily)
    return pd.DataFrame(rows)


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
        name = row.get("スタッフ", "").strip()
        day = row.get("希望休", "").strip()
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


# ---- ヘッダー ----
st.markdown(
    '<div class="main-title">🗓️ 店舗シフト PoC</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="sub-brand">'
    "OpenQARPで試作した店舗シフトPoC ／ "
    "オープンソースの量子アプリケーション開発キット "
    "<b>OpenQARP</b> を用いた学習・営業デモ"
    "</div>",
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="disclaimer-box">'
    "<b>正直な位置づけ</b>："
    "本体の週次シフト最適化は<strong>古典ソルバ</strong>（貪欲＋局所改善）です。"
    "OpenQARP の量子アルゴリズム部品（QAOA）は、"
    "注目日の希望休コンフリクトを縮小した Max-Cut への"
    "<strong>比較・将来拡張デモ</strong>として並べています。"
    "量子が古典に勝つとは主張しません。"
    "既存シフト SaaS の置き換えでもありません。"
    "</div>",
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
            '<span class="badge-ng">OpenQARP 未検出</span> 古典のみ',
            unsafe_allow_html=True,
        )

    st.divider()
    st.markdown("**ブランド**")
    st.caption("OpenQARP — オープンソースの量子アプリケーション開発キット")


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
        width='stretch',
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
    with st.spinner("古典ソルバで週次シフトを作成中…" + ("／ OpenQARP QAOA 比較も実行" if run_quantum else "")):
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
else:
    classical = result["classical"]
    exact = result["exact"]
    quantum = result["quantum"]
    sc = result["scenario"]

    # ---- メトリクス ----
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("スコア（高いほど良い）", f"{classical['score']:.1f}")
    m2.metric("古典ランタイム", f"{classical['seconds']*1000:.2f} ms")
    m3.metric("スタッフ数", len(sc["workers"]))
    pref_ok = sum(1 for h in classical["pref_hits"] if h["granted"])
    pref_all = len(classical["pref_hits"])
    m4.metric("希望休充足", f"{pref_ok}/{pref_all}")

    st.subheader("週次シフト表（古典ソルバ）")
    st.caption("✓希望 = 希望休どおり休み ／ ⚠希望 = 希望を出勤にせざるを得なかった日")
    df = schedule_to_dataframe(classical["schedule"], sc)
    st.dataframe(df, width='stretch', hide_index=True)

    # 希望休バッジ
    st.markdown("**希望休チェック**")
    if classical["pref_hits"]:
        cols = st.columns(min(4, len(classical["pref_hits"])))
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
    st.subheader(f"古典 vs 量子（注目日: {sc['qaoa_focus_day']}）")
    st.caption(
        "量子側は OpenQARP の QAOA で、注目日の希望休コンフリクトを "
        "縮小 Max-Cut に落とした比較デモです。週全体のシフト本体ではありません。"
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("##### 古典・週次から抜粋")
        st.write(f"出勤: {', '.join(classical['focus_on']) or '—'}")
        st.write(f"休み: {', '.join(classical['focus_off']) or '—'}")
        st.caption(f"runtime {classical['seconds']*1000:.2f} ms（週全体）")
    with c2:
        st.markdown("##### 古典・注目日の厳密解")
        st.write(f"出勤: {', '.join(exact['on'])}")
        st.write(f"休み: {', '.join(exact['off'])}")
        st.caption(f"runtime {exact['seconds']*1000:.3f} ms ／ prefスコア {exact['pref_score']:.1f}")
    with c3:
        st.markdown("##### OpenQARP QAOA（縮小）")
        if quantum.get("available"):
            st.write(f"出勤: {', '.join(quantum['qaoa_on'])}")
            st.write(f"休み: {', '.join(quantum['qaoa_off'])}")
            st.caption(
                f"runtime {quantum['qaoa_seconds']:.3f} s ／ "
                f"cut={quantum['qaoa_best_cut']:g} ／ "
                f"prefスコア {quantum['pref_score']:.1f}"
            )
        else:
            st.warning(quantum.get("error", "量子比較をスキップしました。"))

    if quantum.get("available"):
        a1, a2, a3 = st.columns(3)
        with a1:
            st.markdown(
                "QAOA ↔ 古典厳密　"
                + _badge(quantum["agree_exact"], "一致", "不一致"),
                unsafe_allow_html=True,
            )
        with a2:
            st.markdown(
                "QAOA ↔ 週次古典　"
                + _badge(result["agree_week"], "一致", "不一致"),
                unsafe_allow_html=True,
            )
        with a3:
            st.markdown(
                "カット最適内　"
                + _badge(quantum["cut_agree"], "含む", "含まない"),
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
            st.dataframe(pd.DataFrame(freq_rows), hide_index=True, width='stretch')
            st.caption(
                "parameters: "
                + ", ".join(f"{v:.4f}" for v in quantum["parameters"])
            )
            st.caption(
                f"classical Max-Cut best={quantum['cut_exact']:g} "
                f"({quantum['cut_seconds']*1000:.3f} ms)"
            )

    st.markdown(
        '<div class="disclaimer-box">'
        "一致しても「量子が優れている」意味にはなりません。"
        "大規模シフト・労働法規・公平性の厳密モデルは専用の古典ソルバ／"
        "既存アプリ向けです。本 PoC は OpenQARP の部品を店舗オペレーションに"
        "どう載せられるかを見せる試作です。"
        "</div>",
        unsafe_allow_html=True,
    )

# ---- ワンペーシピッチ ----
st.divider()
st.subheader("ワンページ・ピッチ")
if PITCH_MD.exists():
    st.markdown(PITCH_MD.read_text(encoding="utf-8"))
else:
    st.warning("PITCH.md が見つかりません。")

st.caption(
    "© OpenQARP コミュニティ試作 ／ examples/community_mina/pitch ／ "
    "量子優位性は主張しません"
)
