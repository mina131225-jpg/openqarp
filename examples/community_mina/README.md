# Mina の OpenQARP 学習サンプル

OpenQARP の基本的な回路実行と、QAOA による Max-Cut を試す小さな
コマンドラインサンプルです。いずれも学習用デモであり、性能評価・実機の
結果・研究用途の実装を保証するものではありません。

## サンプル

- `bell_sampler.py` — 2量子ビットの Bell 状態を作り、`Sampler` で測定します。
  結果は `00` と `11` がほぼ半分ずつになります（有限ショットの揺らぎがあります）。
- `classical_vs_qaoa_maxcut.py` — 4ノードの重み付きグラフについて、全探索の
  古典的な厳密解と、2層 QAOA の測定結果を比較します。

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
```

ショット数やシードは変更できます。

```bash
python examples/community_mina/bell_sampler.py --shots 1000 --seed 7
python examples/community_mina/classical_vs_qaoa_maxcut.py --shots 1000 --seed 7
```

別のチェックアウト用に作られた venv を共有していて ABI チェックに失敗する
場合だけ、次のように明示的にスキップできます。通常は不要です。

```bash
export QARP_SKIP_ABI_CHECK=1
python examples/community_mina/bell_sampler.py
```

`classical_vs_qaoa_maxcut.py` は `networkx` を使用します。通常の OpenQARP の
依存関係に含まれます。インストール環境によって不足する場合は、次を実行して
ください。

```bash
python -m pip install networkx
```

## 注意

このディレクトリのコードは OpenQARP の使い方を学ぶためのコミュニティサンプル
です。QAOA の結果は最適化・乱数・ショット数に依存します。教育目的以外の判断や
再現性が必要な研究結果には、依存関係とバージョンを固定し、別途検証してください。
