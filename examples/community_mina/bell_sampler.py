#!/usr/bin/env python3
"""Bell 状態を作り、測定結果の確率分布を表示する最小サンプル。"""

from __future__ import annotations

import argparse
import os

# 別チェックアウトの venv を使う場合だけ、README の注意を確認してください。
os.environ.setdefault("QARP_SKIP_ABI_CHECK", "1")

from qarp.algorithms import Sampler
from qarp.blocks import SimpleBlock
from qarp.engines import QarpEngine


def build_bell_state():
    """|00> から (|00> + |11>) / sqrt(2) を準備する。"""
    bell = SimpleBlock(2, name="bell")
    bell.h(0)
    bell.cx(0, 1)
    bell.measure([(0, 0), (1, 1)])
    return bell.build()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shots", type=int, default=2_000, help="測定回数 (default: 2000)")
    parser.add_argument("--seed", type=int, default=42, help="乱数シード (default: 42)")
    args = parser.parse_args()

    if args.shots <= 0:
        parser.error("--shots は正の整数にしてください")

    sampler = Sampler(ket=build_bell_state(), n_shots=args.shots)
    engine = QarpEngine(seed=args.seed)
    engine.build([sampler])
    probabilities = engine.run()[0]

    print("Bell sampler")
    print("============")
    print(f"shots: {args.shots}    seed: {args.seed}")
    print("bitstring (q0, q1)    probability")
    for bits, probability in sorted(probabilities.items(), key=lambda item: item[0]):
        bitstring = "".join(str(bit) for bit in bits)
        print(f"{bitstring:>17}    {probability:.4f}")


if __name__ == "__main__":
    main()
