# LINE 連携ブリッジ（店舗シフト PoC）

OpenQARP コミュニティの **店舗シフト PoC** 向けに、LINE Messaging API へつなぐ薄い層です。

- スタッフが LINE で「希望休 日曜」「シフト見せて」と送る → 古典ソルバで組表 → 返信
- 店長側から週次表をプッシュ（Flex Message / テキスト）
- **Channel secret / token が無いときはデモ／モックモード**（ペイロードをログし、定型返信のみ。実 LINE には送りません）

> このディレクトリ単体では「ライブ LINE が動く」ことは保証しません。  
> 実送受信は、あなたが LINE Developers でチャネルを作り、資格情報を入れた場合のみです。

Credit: Powered by OpenQARP

---

## 1. LINE Messaging API チャネルの作り方

1. [LINE Developers](https://developers.line.biz/console/) にログインする  
2. プロバイダーを作成（または既存を選択）  
3. **Messaging API チャネル**を新規作成  
   - チャネル名: 例「店舗シフトPoC」  
   - 業種・説明は任意  
4. チャネル基本設定で次を控える  
   - **Channel secret**  
5. Messaging API 設定タブで  
   - **チャネルアクセストークン（長期）** を発行して控える  
   - Webhook URL を後述のとおり設定（ngrok 等で HTTPS 公開が必要）  
   - 「Webhookの利用」をオン  
   - 「応答メッセージ」「あいさつメッセージ」はオフ推奨（Bot が reply するため）  
6. 友だち追加用の QR / リンクでテスト用 LINE アカウントを友だちにする  
7. 友だち追加後、webhook の `follow` イベントやメッセージの `source.userId` から **userId** を取得する（プッシュ送信用）

### Webhook URL の例

ローカル開発では ngrok 等でトンネルします。

```bash
# 別ターミナル
ngrok http 8080
# 表示された https://xxxx.ngrok-free.app/webhook を LINE コンソールの Webhook URL に設定
```

検証ボタンで「成功」になれば OK です（本サーバは常に 200 を返します）。

---

## 2. 環境変数

`config.example.env` をコピーして使います。

```bash
cd examples/community_mina/line_bridge
cp config.example.env .env   # .env は git 管理外想定
set -a && source .env && set +a
```

| 変数 | 必須 | 説明 |
|------|------|------|
| `LINE_CHANNEL_SECRET` | 実運用時 | 署名検証用 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 実運用時 | reply / push 用 |
| `LINE_USER_ID` | プッシュ時 | 送信先ユーザー |
| `LINE_DEMO_MODE` | 任意 | `true` で強制デモ（API 非呼び出し） |
| `LINE_WEBHOOK_HOST` / `PORT` | 任意 | 既定 `0.0.0.0:8080` |
| `LINE_SCENARIO_JSON` | 任意 | シナリオ JSON パス |

**未設定のまま起動 → ステータス「未設定」または「デモモード」。実送信しません。**

Streamlit 側では `st.secrets`（例: `.streamlit/secrets.toml`）または同じ環境変数を読めます。

```toml
# .streamlit/secrets.toml の例（ローカルのみ・コミットしない）
LINE_CHANNEL_SECRET = "..."
LINE_CHANNEL_ACCESS_TOKEN = "..."
LINE_USER_ID = "U..."
```

---

## 3. セットアップと起動

```bash
cd /path/to/openqarp
source /path/to/venv/bin/activate   # 例: /workspace/openqarp-env
export QARP_SKIP_ABI_CHECK=1

python -m pip install -r examples/community_mina/line_bridge/requirements.txt

# デモ（資格情報なし）
export LINE_DEMO_MODE=true
python examples/community_mina/line_bridge/webhook_app.py
```

別ターミナルでモック送信:

```bash
# テキスト意図 → メッセージ配列（ソルバ実行あり）
curl -s -X POST http://127.0.0.1:8080/demo/message \
  -H 'Content-Type: application/json' \
  -d '{"text":"シフト見せて","userId":"Udemo"}' | python -m json.tool

# webhook 形式のモック
curl -s -X POST http://127.0.0.1:8080/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [{
      "type": "message",
      "replyToken": "demo-token",
      "source": {"type": "user", "userId": "Udemo"},
      "message": {"type": "text", "text": "希望休 日曜"}
    }]
  }' | python -m json.tool
```

プッシュ生成のみ（サーバ不要）:

```bash
python examples/community_mina/line_bridge/notify.py --demo --flex
python examples/community_mina/line_bridge/notify.py --demo --text
```

---

## 4. 対応テキスト（意図）

| ユーザー発話例 | 動作 |
|----------------|------|
| `シフト見せて` / `シフト表` | 今週のたたき台を Flex（＋短いテキスト）で返信 |
| `希望休 日曜` / `希望休 日` | 先頭スタッフの希望休に「日」を追加して再組表 |
| `希望休 A 土` | スタッフ A の希望休に「土」を追加して再組表 |
| `ヘルプ` | 使い方 |

本体の最適化は **古典ソルバ**（`shift_scheduling_demo` / `solver_bridge`）です。量子比較はこの LINE 層では送りません。

---

## 5. ファイル

| ファイル | 内容 |
|----------|------|
| `webhook_app.py` | Flask webhook。署名検証・follow/message・デモエンドポイント |
| `notify.py` | push / reply。デモ時は JSON をログ |
| `shift_messages.py` | 意図パース・組表・テキスト／Flex 整形・接続ステータス |
| `config.example.env` | 環境変数テンプレ |
| `requirements.txt` | flask, requests |

ピッチ UI との接続は `../pitch/app.py` の「LINE連携」セクションを参照。

---

## 6. 注意（必ず読む）

- **ライブ LINE 動作を資格情報なしで主張しません。** デモモードは UI／営業説明用です。  
- Channel secret / token / userId をリポジトリにコミットしないでください。  
- 署名検証は secret 設定時のみ必須。デモ時はスキップして受理します（ローカル確認用）。  
- ユーザー別希望休はプロセスメモリ保持のみ（再起動で消える PoC）。  
- 労働法規・本格シフト SaaS の代替ではありません。
