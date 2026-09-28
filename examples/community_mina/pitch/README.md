# 店舗シフト PoC（営業デモ）— OpenQARP 試作

**OpenQARPで試作した店舗シフトPoC** の画面・ワンペーシ・売り手トークです。

- **OpenQARP** = オープンソースの量子アプリケーション開発キット  
- **本体の週次シフト** = 古典ソルバ（`shift_scheduling_demo.py` の貪欲＋局所改善）  
- **量子比較** = 注目日の希望休コンフリクトを縮小 Max-Cut にし、OpenQARP の QAOA で並べて表示  
- 量子優位性は主張しません。既存シフト SaaS の置き換えではありません。

## ファイル

| ファイル | 内容 |
|----------|------|
| `app.py` | Streamlit UI（日本語） |
| `solver_bridge.py` | 既存デモへの橋渡し（qarp 欠落時は古典のみ） |
| `PITCH.md` | ワンページ・ピッチ（UI 下部にも表示） |
| `SELLER_SCRIPT.md` | 店舗オーナーへの実トーク原稿 |
| `requirements.txt` | UI 用追加依存（streamlit, pandas） |

## 起動方法

リポジトリルートで、OpenQARP が入った venv を有効化してから:

```bash
cd /path/to/openqarp
source /path/to/venv/bin/activate   # 例: /workspace/openqarp-env
python -m pip install -r examples/community_mina/pitch/requirements.txt

export QARP_SKIP_ABI_CHECK=1   # 別チェックアウトの venv 共有時のみ
streamlit run examples/community_mina/pitch/app.py
```

ブラウザで `http://localhost:8501` が開きます。

ヘッドレスでロジックだけ確認する場合:

```bash
export QARP_SKIP_ABI_CHECK=1
python -c "
from examples.community_mina.pitch.solver_bridge import run_full_demo, default_scenario
r = run_full_demo(default_scenario(), shots=200, seed=1)
print('score', r['classical']['score'], 'qarp', r['qarp_ok'], 'q_ok', r['quantum'].get('available'))
"
```

（`pitch` をカレントにして `from solver_bridge import ...` でも可。）

## 30秒デモ手順（売り手）

1. 上記でアプリを起動し、既定の 4 人シナリオのままにする。  
2. **「シフトを組む」** を押す。  
3. 週次表・希望休バッジ・スコアを見せる。  
4. 「古典 vs 量子」で OpenQARP QAOA の結果と一致バッジを指す。  
5. 必ず言う: **本体は古典。量子は比較・将来拡張。勝ち主張しない。SaaS 置き換えではない。**  
6. 詳細トークは `SELLER_SCRIPT.md` をそのまま読む。

## 注意

- `QARP_SKIP_ABI_CHECK=1` は、公式以外のビルド／別ツリーの venv 共有時の回避策です。通常の `pip install -e .` では不要なことが多いです。  
- qarp が import できない場合、UI は古典のみで動作し、量子パネルに理由を表示します。
