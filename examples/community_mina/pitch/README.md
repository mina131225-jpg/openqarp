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
| `../line_bridge/` | LINE Messaging API ブリッジ（webhook・notify・日本語手順） |

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

1. 上記でアプリを起動する（詳細設定は折りたたみのまま）。  
2. **「サンプル店で今すぐ組む」** を押す（`shift_scenario_tiny.json` を自動読込）。  
3. 提案サマリー・週次表・希望休バッジ・スコアを見せる。  
4. Before/After と「古典 vs 量子」で OpenQARP QAOA の一致バッジを指す。  
5. 必ず言う: **本体は古典。量子は比較・将来拡張。勝ち主張しない。SaaS 置き換えではない。**  
6. **LINE連携**でステータス（未設定／デモモード）と「LINE向けメッセージを生成」プレビューを見せる。  
   ライブ送信は主張しない（資格情報があるときだけテスト送信可）。  
7. 詳細トークは `SELLER_SCRIPT.md`（先頭の15秒フックから）をそのまま読む。

## 価格感・次の一歩（仮）

- お試し PoC: 画面合わせ **0〜数万円／回（仮）**
- 店舗カスタム: **月額数万円〜（仮）**
- CTA: 「まず30分の画面合わせから」→ 感触OKならカスタム仮見積
- Credit: **Powered by OpenQARP**

正式見積ではありません。詳細は `PITCH.md` / `SELLER_SCRIPT.md`。

## 注意

- `QARP_SKIP_ABI_CHECK=1` は、公式以外のビルド／別ツリーの venv 共有時の回避策です。通常の `pip install -e .` では不要なことが多いです。  
- qarp が import できない場合、UI は古典のみで動作し、量子パネルに理由を表示します。  
- LINE 実送信には Channel secret / token / userId が必要です。未設定時はデモ／プレビューのみ（`line_bridge/README.md`）。
