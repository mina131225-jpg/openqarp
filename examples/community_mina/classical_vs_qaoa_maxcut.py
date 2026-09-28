#!/usr/bin/env python3
"""小さな重み付きグラフで、厳密解と QAOA の Max-Cut を比較する。"""

from __future__ import annotations

import argparse
import itertools
import os
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from time import perf_counter

# 別チェックアウトの venv を使う場合だけ、README の注意を確認してください。
os.environ.setdefault("QARP_SKIP_ABI_CHECK", "1")

import networkx as nx

from qarp import config
from qarp.algorithms import QAOA, Sampler
from qarp.engines import QarpEngine

N_NODES = 4
N_LAYERS = 2
DEFAULT_SHOTS = 2_000
# 公式 Max-Cut 例と同じ、4 ノード・重み 2 の 2 辺。
EDGES = ((0, 3, 2.0), (0, 2, 2.0))


def make_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(range(N_NODES))
    graph.add_weighted_edges_from(EDGES)
    return graph


def bitstring(bits) -> str:
    """タプルと文字列のどちらも表示用の文字列にする。"""
    if isinstance(bits, str):
        return bits
    return "".join(str(int(bit)) for bit in bits)


def cut_value(graph: nx.Graph, bits: str) -> float:
    """ビット列が表すカットの重みを返す。"""
    return sum(
        data.get("weight", 1.0)
        for u, v, data in graph.edges(data=True)
        if int(bits[u]) != int(bits[v])
    )


def classical_exact(graph: nx.Graph) -> tuple[float, list[str], float]:
    """全ビット列を調べて、厳密な Max-Cut 解を求める。"""
    started = perf_counter()
    scored = [
        (cut_value(graph, bits), bitstring(bits))
        for bits in itertools.product((0, 1), repeat=N_NODES)
    ]
    best_value = max(value for value, _ in scored)
    best_bits = sorted(bits for value, bits in scored if value == best_value)
    return best_value, best_bits, perf_counter() - started


def qaoa_run(graph: nx.Graph, shots: int, seed: int):
    """QAOA を最適化し、最終状態をサンプリングする。"""
    started = perf_counter()
    config.seed = seed
    qaoa = QAOA(
        problem=graph,
        n_layers=N_LAYERS,
        use_rzz=True,
        verbose=False,
        initial_parameters=[0.0] * (2 * N_LAYERS),
    ).build()
    energy, parameters = qaoa.run()

    # 最適化後の回路をコピーしてから測定を追加する。
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
        parameters,
        ranked,
        best_sample_value,
        best_sample_bits,
        perf_counter() - started,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shots", type=int, default=DEFAULT_SHOTS, help="QAOA の測定回数")
    parser.add_argument("--seed", type=int, default=1234, help="乱数シード")
    args = parser.parse_args()
    if args.shots <= 0:
        parser.error("--shots は正の整数にしてください")

    graph = make_graph()
    classical_value, classical_bits, classical_seconds = classical_exact(graph)

    # 最適化ライブラリの進捗表示を隠し、比較結果を読みやすくする。
    quiet = StringIO()
    with redirect_stdout(quiet), redirect_stderr(quiet):
        (
            qaoa_energy,
            parameters,
            frequencies,
            qaoa_best_value,
            qaoa_best_bits,
            qaoa_seconds,
        ) = qaoa_run(graph, args.shots, args.seed)

    total_weight = sum(weight for _, _, weight in EDGES)
    expected_cut = (total_weight - qaoa_energy) / 2.0
    agreement = set(qaoa_best_bits).issubset(classical_bits)

    print("Classical vs QAOA Max-Cut")
    print("==========================")
    print(f"nodes: {N_NODES}    shots: {args.shots}    seed: {args.seed}")
    print("edges (u, v, weight): " + ", ".join(f"({u}, {v}, {w:g})" for u, v, w in EDGES))

    print("\nSample frequencies (descending):")
    for bits, count in frequencies:
        print(f"  {bits}: {count:4d} ({count / args.shots:.3f})  cut={cut_value(graph, bits):g}")

    print("\nResults")
    print(f"  classical exact best cut : {classical_value:g}")
    print(f"  classical best bitstrings: {', '.join(classical_bits)}")
    print(f"  QAOA best sampled cut    : {qaoa_best_value:g}")
    print(f"  QAOA best bitstrings     : {', '.join(qaoa_best_bits)}")
    print(f"  QAOA energy              : {qaoa_energy:.6f}")
    print(f"  expected cut from energy : {expected_cut:.6f}")
    print(f"  agreement with optimum   : {'yes' if agreement else 'no'}")
    print(f"  runtime (classical/QAOA) : {classical_seconds:.6f}s / {qaoa_seconds:.6f}s")
    print("  optimized parameters     : " + ", ".join(f"{float(value):.6f}" for value in parameters))


if __name__ == "__main__":
    main()
