# Mina の OpenQARP 学習サンプル

OpenQARP の基本的な回路実行と、QAOA による Max-Cut、およびそれらを
小さなシフト割当プロトタイプへ発展させたコミュニティサンプルです。
いずれも**学習用デモ**であり、性能評価・実機結果・研究用途・実運用の
シフト自動化を保証するものではありません。**量子優位性も主張しません。**

## サンプルの進化

当初の `bell_sampler.py` / `classical_vs_qaoa_maxcut.py` は、測定と
小さな Max-Cut の「おもちゃデモ」でした。`shift_scheduling_demo.py` は
同じ OpenQARP の QAOA パターンを再利用しつつ、次のように一歩実用寄りの
題材へ寄せています。

1. **古典**: 4 人 × 7 日の店舗シナリオ (最低人数・連続勤務上限・希望休) を
   貪欲 + 局所改善で解き、読みやすいシフト表を表示する。
2. **縮小量子**: 注目日 (既定は「日」) の希望休コンフリクトだけを
   4 ノードの重み付き Max-Cut に落とし、既存の QAOA 手順で解く。
   量子ビット数は意図的に小さく、CPU で数秒以内を目標とする。
3. **並べて表示**: 古典の週次表、縮小問題の厳密解、QAOA 結果、一致メモ、
   実行時間を並べる。一致しても量子が優れている意味にはならない。

実店舗のシフトアプリ、労働法規、公平性の厳密モデル、大規模インスタンスには
対応していません。専用の古典ソルバや既存サービスを使ってください。

## サンプル一覧

- `bell_sampler.py` — 2 量子ビットの Bell 状態を作り、`Sampler` で測定します。
  結果は `00` と `11` がほぼ半分ずつになります（有限ショットの揺らぎがあります）。
- `classical_vs_qaoa_maxcut.py` — 4 ノードの重み付きグラフについて、全探索の
  古典的な厳密解と、2 層 QAOA の測定結果を比較します。
- `shift_scheduling_demo.py` — 上記の発展版。小さなシフト割当を古典で解き、
  縮小版を Max-Cut / QAOA に対応付けて並べて表示します。
- `shift_scenario_tiny.json` — シフトデモ用のシナリオ例 (JSON)。スクリプト内の
  既定 dict と同じ内容です。`--scenario` で差し替えできます。
- `pitch/` — **OpenQARPで試作した店舗シフトPoC** の営業デモ (Streamlit UI・ワンペーシ・売り手トーク)。
  本体は古典、比較に OpenQARP QAOA。起動は `pitch/README.md` を参照。

## 実行方法

リポジトリのルートで venv を作成し、OpenQARP をインストールします。
Python 3.11 以降を使用してください。

```bash
cd /path/to/openqarp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .

python examples/community_mina/bell_sampler.py
python examples/community_mina/classical_vs_qaoa_maxcut.py
python examples/community_mina/shift_scheduling_demo.py

# 営業向け UI (streamlit / pandas が必要)
python -m pip install -r examples/community_mina/pitch/requirements.txt
streamlit run examples/community_mina/pitch/app.py
```

ショット数やシード、シナリオ JSON は変更できます。

```bash
python examples/community_mina/bell_sampler.py --shots 1000 --seed 7
python examples/community_mina/classical_vs_qaoa_maxcut.py --shots 1000 --seed 7
python examples/community_mina/shift_scheduling_demo.py --shots 1000 --seed 7
python examples/community_mina/shift_scheduling_demo.py \
  --scenario examples/community_mina/shift_scenario_tiny.json
```

別のチェックアウト用に作られた venv を共有していて ABI チェックに失敗する
場合だけ、次のように明示的にスキップできます。通常は不要です。
シフトデモと Max-Cut デモはスクリプト内でも `QARP_SKIP_ABI_CHECK=1` を
既定でセットします。

```bash
export QARP_SKIP_ABI_CHECK=1
python examples/community_mina/bell_sampler.py
```

`classical_vs_qaoa_maxcut.py` と `shift_scheduling_demo.py` は `networkx` を
使用します。通常の OpenQARP の依存関係に含まれます。不足する場合は次を
実行してください。

```bash
python -m pip install networkx
```

## 注意

このディレクトリのコードは OpenQARP の使い方を学ぶためのコミュニティサンプル
です。QAOA の結果は最適化・乱数・ショット数に依存します。教育目的以外の判断や
再現性が必要な研究結果には、依存関係とバージョンを固定し、別途検証してください。
シフトデモの Max-Cut 対応は教育用の縮小であり、実シフト最適化の代替ではありません。
