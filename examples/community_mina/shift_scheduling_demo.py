#!/usr/bin/env python3
"""小さな店舗シフトを古典解と、縮小版 Max-Cut / QAOA で並べて見るデモ。

注意: これは学習用プロトタイプです。実運用のシフト自動作成アプリには
勝てません。量子優位性も主張しません。量子ビット数を意図的に小さくし、
CPU 上で数秒以内に終わるようにしています。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from time import perf_counter
from typing import Any

# 別チェックアウトの venv を使う場合だけ、README の注意を確認してください。
os.environ.setdefault("QARP_SKIP_ABI_CHECK", "1")

import networkx as nx

# qarp は QAOA パスでのみ必要。欠落時は古典ソルバだけ動かす。
_QARP_IMPORT_ERROR: Exception | None = None


def _ensure_qarp():
    """遅延 import。失敗時は RuntimeError を投げる。"""
    global _QARP_IMPORT_ERROR
    try:
        from qarp import config
        from qarp.algorithms import QAOA, Sampler
        from qarp.engines import QarpEngine
        return config, QAOA, Sampler, QarpEngine
    except Exception as exc:  # noqa: BLE001 — デモ用に広く捕捉
        _QARP_IMPORT_ERROR = exc
        raise RuntimeError(
            f"qarp を import できません ({exc}). "
            "古典ソルバのみ利用可能です。QARP_SKIP_ABI_CHECK=1 も確認してください。"
        ) from exc


def qarp_available() -> bool:
    try:
        _ensure_qarp()
        return True
    except RuntimeError:
        return False

# ---------------------------------------------------------------------------
# 既定シナリオ: 4 人 × 7 日の小さな店舗
# ---------------------------------------------------------------------------
DEFAULT_SCENARIO: dict[str, Any] = {
    "workers": ["A", "B", "C", "D"],
    "days": ["月", "火", "水", "木", "金", "土", "日"],
    "min_staff_per_day": 2,
    "max_consecutive_days": 4,
    # 希望休 (preferred offs)。満たせるとスコアが上がるが、必須ではない。
    "preferred_offs": {
        "A": ["日"],
        "B": ["土"],
        "C": ["水"],
        "D": ["月", "日"],
    },
    # QAOA に渡す縮小問題の対象日 (この日の希望休コンフリクトを Max-Cut 化)
    "qaoa_focus_day": "日",
    "qaoa_needed_on_focus": 2,
}

N_LAYERS = 2
DEFAULT_SHOTS = 1_000


def load_scenario(path: str | None) -> dict[str, Any]:
    """JSON ファイルがあれば読み、なければ既定の dict を返す。"""
    if path is None:
        return deepcopy(DEFAULT_SCENARIO)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    merged = deepcopy(DEFAULT_SCENARIO)
    merged.update(data)
    return merged


def validate_scenario(scenario: dict[str, Any]) -> None:
    workers = scenario["workers"]
    days = scenario["days"]
    if len(workers) < 2 or len(days) < 1:
        raise ValueError("workers は 2 人以上、days は 1 日以上が必要です")
    if scenario["min_staff_per_day"] > len(workers):
        raise ValueError("min_staff_per_day が労働者数を超えています")
    focus = scenario["qaoa_focus_day"]
    if focus not in days:
        raise ValueError(f"qaoa_focus_day={focus!r} が days にありません")
    prefs = scenario.get("preferred_offs", {})
    for name, offs in prefs.items():
        if name not in workers:
            raise ValueError(f"preferred_offs の未知の労働者: {name}")
        for day in offs:
            if day not in days:
                raise ValueError(f"{name} の希望休 {day!r} が days にありません")


# ---------------------------------------------------------------------------
# 古典ソルバ: 貪欲 + スコア改善 (ヒューリスティック)
# ---------------------------------------------------------------------------
def score_schedule(
    schedule: dict[str, list[bool]],
    scenario: dict[str, Any],
) -> float:
    """制約違反を罰し、希望休の充足を加点するスコア (大きいほど良い)。"""
    workers = scenario["workers"]
    days = scenario["days"]
    min_staff = scenario["min_staff_per_day"]
    max_consec = scenario["max_consecutive_days"]
    prefs = scenario.get("preferred_offs", {})

    score = 0.0

    # 日ごとの最低人数
    for d_idx, _day in enumerate(days):
        on_count = sum(1 for w in workers if schedule[w][d_idx])
        if on_count < min_staff:
            score -= 100.0 * (min_staff - on_count)
        else:
            score += 1.0  # 充足ボーナス

    # 連続勤務上限
    for w in workers:
        run = 0
        for on in schedule[w]:
            if on:
                run += 1
                if run > max_consec:
                    score -= 50.0
            else:
                run = 0

    # 希望休
    for w, off_days in prefs.items():
        for day in off_days:
            d_idx = days.index(day)
            if not schedule[w][d_idx]:
                score += 10.0
            else:
                score -= 3.0  # 希望を潰した軽い罰

    # 公平性: 勤務日数のばらつきを少し抑える
    loads = [sum(schedule[w]) for w in workers]
    avg = sum(loads) / len(loads)
    score -= sum((load - avg) ** 2 for load in loads)

    return score


def classical_greedy(scenario: dict[str, Any]) -> tuple[dict[str, list[bool]], float, float]:
    """日ごとに必要人数を埋め、希望休を優先する貪欲法。"""
    started = perf_counter()
    workers = scenario["workers"]
    days = scenario["days"]
    min_staff = scenario["min_staff_per_day"]
    prefs = scenario.get("preferred_offs", {})

    schedule = {w: [False] * len(days) for w in workers}

    for d_idx, day in enumerate(days):
        # 希望休の人は後ろ、そうでない人は前に並べる
        ranked = sorted(
            workers,
            key=lambda w: (
                1 if day in prefs.get(w, []) else 0,  # 希望休なら後回し
                sum(schedule[w][:d_idx]),  # これまでの勤務が少ない人を優先
                w,
            ),
        )
        for w in ranked:
            if sum(1 for x in workers if schedule[x][d_idx]) >= min_staff:
                break
            # 連続上限チェック
            run = 0
            for on in reversed(schedule[w][:d_idx]):
                if on:
                    run += 1
                else:
                    break
            if run >= scenario["max_consecutive_days"]:
                continue
            schedule[w][d_idx] = True

        # まだ足りなければ制約を緩めて埋める
        for w in ranked:
            if sum(1 for x in workers if schedule[x][d_idx]) >= min_staff:
                break
            schedule[w][d_idx] = True

    # 局所改善: 1 ビット反転でスコアが上がれば採用 (数パス)
    best = score_schedule(schedule, scenario)
    improved = True
    passes = 0
    while improved and passes < 3:
        improved = False
        passes += 1
        for w in workers:
            for d_idx in range(len(days)):
                schedule[w][d_idx] = not schedule[w][d_idx]
                new_score = score_schedule(schedule, scenario)
                if new_score > best:
                    best = new_score
                    improved = True
                else:
                    schedule[w][d_idx] = not schedule[w][d_idx]

    elapsed = perf_counter() - started
    return schedule, best, elapsed


def classical_exact_focus(
    scenario: dict[str, Any],
) -> tuple[list[str], list[str], float]:
    """縮小問題 (注目日に誰を休ませるか) を全列挙で解く。

    注目日に必要な出勤人数を満たしつつ、希望休の充足を最大化する。
    """
    started = perf_counter()
    workers = scenario["workers"]
    focus = scenario["qaoa_focus_day"]
    need_on = scenario["qaoa_needed_on_focus"]
    prefs = scenario.get("preferred_offs", {})

    best_score = float("-inf")
    best_off: list[str] = []
    best_on: list[str] = []

    for on_combo in itertools.combinations(workers, need_on):
        on_set = set(on_combo)
        off_set = [w for w in workers if w not in on_set]
        score = 0.0
        for w in off_set:
            if focus in prefs.get(w, []):
                score += 10.0
        for w in on_set:
            if focus in prefs.get(w, []):
                score -= 3.0
        # 公平寄り: 希望休を持つ人をできるだけ休ませる
        if score > best_score or (
            score == best_score and off_set < best_off
        ):
            best_score = score
            best_off = off_set
            best_on = sorted(on_set)

    return best_on, best_off, perf_counter() - started


# ---------------------------------------------------------------------------
# 縮小問題 → Max-Cut / QAOA
# ---------------------------------------------------------------------------
def build_focus_conflict_graph(scenario: dict[str, Any]) -> nx.Graph:
    """注目日の希望休を Max-Cut グラフに対応付ける (教育用の縮小)。

    ノード = 労働者。
    希望休を持つ人どうし・持たない人どうしは同じ側に置きたいので弱い辺、
    「希望あり ↔ 希望なし」は反対側に置きたいので強い辺にする。
    こうすると、希望休者をまとめて休ませる 2-2 分割が Max-Cut 最適に
    寄りやすい (完全なシフト制約の代替ではない)。
    """
    workers = scenario["workers"]
    focus = scenario["qaoa_focus_day"]
    prefs = scenario.get("preferred_offs", {})
    n = len(workers)
    graph = nx.Graph()
    graph.add_nodes_from(range(n))

    wants_off = [focus in prefs.get(w, []) for w in workers]
    for i, j in itertools.combinations(range(n), 2):
        if wants_off[i] == wants_off[j]:
            # 同じ希望グループ → 同じ側が望ましい
            weight = 0.5
        else:
            # 希望ありとなし → 反対側が望ましい
            weight = 3.0
        graph.add_edge(i, j, weight=weight)
    return graph


def bitstring(bits) -> str:
    if isinstance(bits, str):
        return bits
    return "".join(str(int(bit)) for bit in bits)


def cut_value(graph: nx.Graph, bits: str) -> float:
    return sum(
        data.get("weight", 1.0)
        for u, v, data in graph.edges(data=True)
        if int(bits[u]) != int(bits[v])
    )


def focus_preference_score(
    on: list[str],
    off: list[str],
    scenario: dict[str, Any],
) -> float:
    """注目日の希望休充足スコア (大きいほど良い)。"""
    focus = scenario["qaoa_focus_day"]
    prefs = scenario.get("preferred_offs", {})
    score = 0.0
    for w in off:
        score += 10.0 if focus in prefs.get(w, []) else 0.0
    for w in on:
        score -= 3.0 if focus in prefs.get(w, []) else 0.0
    return score


def partition_from_bits(
    bits: str,
    workers: list[str],
    need_on: int,
    scenario: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """ビット列の 0/1 分割を、必要出勤人数に合わせて on/off に割り当てる。"""
    group0 = [workers[i] for i, b in enumerate(bits) if b == "0"]
    group1 = [workers[i] for i, b in enumerate(bits) if b == "1"]

    candidates: list[tuple[list[str], list[str]]] = []
    if len(group0) == need_on:
        candidates.append((sorted(group0), sorted(group1)))
    if len(group1) == need_on:
        candidates.append((sorted(group1), sorted(group0)))
    if not candidates:
        # バランスが崩れている場合は、大きい側から need_on 人を出勤にする
        larger = group0 if len(group0) >= len(group1) else group1
        smaller = group1 if larger is group0 else group0
        # 希望休を優先して休ませるよう、希望者を後ろへ
        if scenario is not None:
            focus = scenario["qaoa_focus_day"]
            prefs = scenario.get("preferred_offs", {})
            larger = sorted(
                larger,
                key=lambda w: (1 if focus in prefs.get(w, []) else 0, w),
            )
        on = sorted(larger[:need_on])
        off = sorted(list(larger[need_on:]) + list(smaller))
        return on, off

    if scenario is not None:
        candidates.sort(
            key=lambda pair: (
                -focus_preference_score(pair[0], pair[1], scenario),
                pair[0],
                pair[1],
            )
        )
    else:
        candidates.sort(key=lambda pair: (pair[0], pair[1]))
    return candidates[0]


def classical_maxcut_exact(graph: nx.Graph) -> tuple[float, list[str], float]:
    started = perf_counter()
    n = graph.number_of_nodes()
    scored = [
        (cut_value(graph, bitstring(bits)), bitstring(bits))
        for bits in itertools.product((0, 1), repeat=n)
    ]
    best_value = max(value for value, _ in scored)
    best_bits = sorted(bits for value, bits in scored if value == best_value)
    return best_value, best_bits, perf_counter() - started


def qaoa_maxcut(
    graph: nx.Graph,
    shots: int,
    seed: int,
) -> tuple[float, list, list[tuple[str, int]], float, list[str], float]:
    started = perf_counter()
    config, QAOA, Sampler, QarpEngine = _ensure_qarp()
    config.seed = seed
    qaoa = QAOA(
        problem=graph,
        n_layers=N_LAYERS,
        use_rzz=True,
        verbose=False,
        initial_parameters=[0.0] * (2 * N_LAYERS),
    ).build()
    energy, parameters = qaoa.run()

    circuit = deepcopy(qaoa.get_final_state_block())
    circuit.measure([(qubit, qubit) for qubit in range(circuit.n_qubits)])
    sampler = Sampler(ket=circuit, n_shots=shots)
    engine = QarpEngine(seed=seed)
    engine.build([sampler])
    probabilities = engine.run()[0]

    frequencies = Counter(
        {
            bitstring(bits): int(round(float(probability) * shots))
            for bits, probability in probabilities.items()
        }
    )
    ranked = sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))
    best_sample_value = max(cut_value(graph, bits) for bits, _ in ranked)
    best_sample_bits = sorted(
        bits for bits, _ in ranked if cut_value(graph, bits) == best_sample_value
    )
    return (
        float(energy),
        list(parameters),
        ranked,
        best_sample_value,
        best_sample_bits,
        perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
def format_table(schedule: dict[str, list[bool]], scenario: dict[str, Any]) -> str:
    workers = scenario["workers"]
    days = scenario["days"]
    col_w = max(4, max(len(d) for d in days))
    name_w = max(len(w) for w in workers + ["労働者"])

    header = f"{'労働者':<{name_w}} | " + " ".join(f"{d:^{col_w}}" for d in days) + " | 出勤"
    sep = "-" * len(header)
    lines = [header, sep]
    for w in workers:
        cells = []
        for on in schedule[w]:
            cells.append(f"{'○' if on else '／':^{col_w}}")
        load = sum(schedule[w])
        lines.append(f"{w:<{name_w}} | " + " ".join(cells) + f" | {load}")

    # 日ごとの人数
    daily = []
    for d_idx in range(len(days)):
        count = sum(1 for w in workers if schedule[w][d_idx])
        daily.append(f"{count:^{col_w}}")
    lines.append(sep)
    lines.append(f"{'人数':<{name_w}} | " + " ".join(daily) + " |")
    return "\n".join(lines)


def print_disclaimer() -> None:
    print(
        "【免責】本デモは OpenQARP 学習用のシフト試作です。"
        "実店舗のシフトアプリ・最適化ソルバには勝てません。"
        "量子優位性も主張しません。縮小 Max-Cut は教育用の対応付けです。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        type=str,
        default=None,
        help="シナリオ JSON パス (省略時はスクリプト内の既定 dict)",
    )
    parser.add_argument("--shots", type=int, default=DEFAULT_SHOTS, help="QAOA 測定回数")
    parser.add_argument("--seed", type=int, default=1234, help="乱数シード")
    args = parser.parse_args()
    if args.shots <= 0:
        parser.error("--shots は正の整数にしてください")

    scenario = load_scenario(args.scenario)
    validate_scenario(scenario)

    workers = scenario["workers"]
    focus = scenario["qaoa_focus_day"]
    need_on = scenario["qaoa_needed_on_focus"]
    prefs = scenario.get("preferred_offs", {})

    print("Shift scheduling prototype (classical + reduced QAOA Max-Cut)")
    print("=" * 64)
    print_disclaimer()
    print()
    print(f"workers: {', '.join(workers)}")
    print(f"days   : {', '.join(scenario['days'])}")
    print(f"min_staff_per_day={scenario['min_staff_per_day']}  "
          f"max_consecutive_days={scenario['max_consecutive_days']}")
    print("preferred_offs:")
    for w in workers:
        offs = prefs.get(w, [])
        print(f"  {w}: {', '.join(offs) if offs else '(なし)'}")
    print(f"QAOA focus day: {focus}  (need {need_on} on duty)")
    print(f"shots={args.shots}  seed={args.seed}  qubits={len(workers)}")
    print()

    # --- 古典: フル週の貪欲+改善 ---
    schedule, classical_score, classical_seconds = classical_greedy(scenario)
    print("--- Classical (greedy + local improve) full-week table ---")
    print(format_table(schedule, scenario))
    print(f"score={classical_score:.2f}  runtime={classical_seconds:.6f}s")
    focus_idx = scenario["days"].index(focus)
    classical_focus_on = sorted(w for w in workers if schedule[w][focus_idx])
    classical_focus_off = sorted(w for w in workers if not schedule[w][focus_idx])
    print(f"classical on {focus}: {', '.join(classical_focus_on) or '(なし)'}")
    print(f"classical off {focus}: {', '.join(classical_focus_off) or '(なし)'}")
    print()

    # --- 古典厳密: 注目日だけ ---
    exact_on, exact_off, exact_seconds = classical_exact_focus(scenario)
    print("--- Classical exact on reduced focus-day problem ---")
    print(f"exact on {focus}: {', '.join(exact_on)}")
    print(f"exact off {focus}: {', '.join(exact_off)}")
    print(f"runtime={exact_seconds:.6f}s")
    print()

    # --- Max-Cut / QAOA ---
    graph = build_focus_conflict_graph(scenario)
    cut_exact, cut_bits, cut_seconds = classical_maxcut_exact(graph)

    quiet = StringIO()
    with redirect_stdout(quiet), redirect_stderr(quiet):
        (
            qaoa_energy,
            parameters,
            frequencies,
            qaoa_best_cut,
            qaoa_best_bits,
            qaoa_seconds,
        ) = qaoa_maxcut(graph, args.shots, args.seed)

    # 最適カットのビット列のうち、希望休スコアが最良の解釈を採用
    def best_partition(bit_list: list[str]) -> tuple[str, list[str], list[str]]:
        ranked_parts = []
        for bits in bit_list:
            on, off = partition_from_bits(bits, workers, need_on, scenario)
            ranked_parts.append(
                (-focus_preference_score(on, off, scenario), bits, on, off)
            )
        ranked_parts.sort()
        _, bits, on, off = ranked_parts[0]
        return bits, on, off

    qaoa_bits, qaoa_on, qaoa_off = best_partition(qaoa_best_bits)
    _, exact_cut_on, exact_cut_off = best_partition(cut_bits)

    print("--- Reduced Max-Cut / QAOA (focus-day conflict graph) ---")
    print(
        "edges: "
        + ", ".join(
            f"({workers[u]}-{workers[v]}, w={data.get('weight', 1):g})"
            for u, v, data in graph.edges(data=True)
        )
    )
    print("sample frequencies (top):")
    for bits, count in frequencies[:8]:
        on, off = partition_from_bits(bits, workers, need_on, scenario)
        print(
            f"  {bits}: {count:4d} ({count / args.shots:.3f})  "
            f"cut={cut_value(graph, bits):g}  "
            f"on={{{','.join(on)}}} off={{{','.join(off)}}}"
        )
    print(f"classical Max-Cut best : {cut_exact:g}  bits={', '.join(cut_bits)}")
    print(f"QAOA best sampled cut  : {qaoa_best_cut:g}  bits={', '.join(qaoa_best_bits)}")
    print(f"QAOA energy            : {qaoa_energy:.6f}")
    print(f"QAOA on {focus}            : {', '.join(qaoa_on)}")
    print(f"QAOA off {focus}           : {', '.join(qaoa_off)}")
    print(
        "optimized parameters   : "
        + ", ".join(f"{float(v):.6f}" for v in parameters)
    )
    print(f"runtime (MaxCut exact / QAOA): {cut_seconds:.6f}s / {qaoa_seconds:.6f}s")
    print()

    # --- 合意メモ ---
    print("--- Side-by-side / agreement notes ---")
    focus_match_exact = set(qaoa_on) == set(exact_on)
    focus_match_week = set(qaoa_on) == set(classical_focus_on)
    cut_agree = set(qaoa_best_bits).issubset(set(cut_bits))
    print(f"QAOA vs classical-exact focus on-set : "
          f"{'agree' if focus_match_exact else 'differ'}  "
          f"(exact={{{','.join(exact_on)}}} qaoa={{{','.join(qaoa_on)}}})")
    print(f"QAOA vs full-week classical {focus} on-set : "
          f"{'agree' if focus_match_week else 'differ'}  "
          f"(week={{{','.join(classical_focus_on)}}} qaoa={{{','.join(qaoa_on)}}})")
    print(f"QAOA bitstrings ⊆ classical Max-Cut optima : "
          f"{'yes' if cut_agree else 'no'}")
    print(
        f"runtimes: classical-week={classical_seconds:.6f}s  "
        f"classical-focus={exact_seconds:.6f}s  "
        f"maxcut-exact={cut_seconds:.6f}s  "
        f"qaoa={qaoa_seconds:.6f}s"
    )
    print()
    print(
        "解釈メモ: フル週の古典表はヒューリスティックです。"
        "QAOA は注目日の希望休コンフリクトを 4 ノード Max-Cut に落とした縮小問題だけを解きます。"
        "一致しても量子が優れている意味にはなりません。"
        "大規模シフトや労働法規対応には、専用の古典ソルバ / 既存アプリを使ってください。"
    )
    print_disclaimer()


if __name__ == "__main__":
    main()
