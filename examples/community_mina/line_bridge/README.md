# LINE 連携ブリッジ（店舗シフト PoC・マルチテナント）

OpenQARP コミュニティの **店舗シフト PoC** 向けに、LINE Messaging API へつなぐ薄い層です。

## 販売時の前提（重要）

| 使うもの | 使わないもの |
|----------|--------------|
| **お客様（店舗）の LINE 公式アカウント** | 開発者個人の LINE アカウント |
| 店長が発行する **店舗コード（招待コード）** | 開発者の userId 固定 |
| スタッフが公式を友だち追加 →「登録 店舗コード」 | 開発者が一人ずつ手登録 |

**開発者個人 LINE は不要です。**  
販売・導入時は、店舗オーナー側で LINE Developers に Messaging API チャネルを作り、Channel secret / token を設定します。スタッフは公式アカウントを友だち追加し、店長から受け取ったコードで登録します。

Credit: Powered by OpenQARP

---

## 店舗オンボーディングの流れ

```
店長（Streamlit「店舗向け」）
  │  店舗を作成 → 招待コード／QRプレースホルダ URL を配布
  ▼
スタッフ
  │  お客様の LINE 公式を友だち追加
  │  「登録 MINA01」  ← 店舗コードで userId を紐付け
  ▼
日常
  │  「希望休 日曜」→ その店舗のシナリオに反映して再組表
  │  「シフト見せて」→ その店舗の週次表を返信
  ▼
店長
     組表生成 → 登録メンバー全員へブロードキャスト（デモ可）
```

店舗データは `stores.json`（または `LINE_STORES_PATH`）に保存されます。

| フィールド | 内容 |
|------------|------|
| `store_id` | 内部 ID（例: `store_a1b2c3d4`） |
| `store_name` | 表示名 |
| `invite_code` | 招待コード（例: `DEMO01`） |
| `line_user_ids[]` | 登録済みスタッフの LINE userId |
| `preferences` | 希望休など店舗共有設定 |

---

## 1. LINE Messaging API チャネルの作り方（お客様側）

1. [LINE Developers](https://developers.line.biz/console/) に **お客様（店舗）のアカウント** でログイン  
2. プロバイダーを作成（または既存を選択）  
3. **Messaging API チャネル**を新規作成  
4. **Channel secret** と **チャネルアクセストークン（長期）** を控える  
5. Webhook URL を設定（ngrok 等で HTTPS 公開）し、「Webhookの利用」をオン  
6. 「応答メッセージ」「あいさつメッセージ」はオフ推奨  
7. 友だち追加用 QR／リンクをスタッフに配布  

> 開発者の個人 LINE で運用しないでください。販売先の公式を使います。

### Webhook URL の例

```bash
ngrok http 8080
# https://xxxx.ngrok-free.app/webhook を LINE コンソールへ
```

---

## 2. 環境変数

`config.example.env` をコピーして使います。

```bash
cd examples/community_mina/line_bridge
cp config.example.env .env
set -a && source .env && set +a
```

| 変数 | 必須 | 説明 |
|------|------|------|
| `LINE_CHANNEL_SECRET` | 実運用時 | 署名検証用（**顧客の公式**） |
| `LINE_CHANNEL_ACCESS_TOKEN` | 実運用時 | reply / push 用 |
| `LINE_USER_ID` | 任意 | 単一宛先プッシュ用（マルチテナントでは店舗の `line_user_ids`） |
| `LINE_DEMO_MODE` | 任意 | `true` で強制デモ |
| `LINE_STORES_PATH` | 任意 | 店舗 JSON パス（既定 `stores.json`） |
| `LINE_INVITE_BASE_URL` | 任意 | QR プレースホルダ用の友だち追加 URL ベース |
| `LINE_REQUIRE_REGISTER` | 任意 | 未登録に希望休／シフトを拒否（既定 true） |
| `LINE_WEBHOOK_HOST` / `PORT` | 任意 | 既定 `0.0.0.0:8080` |

**未設定のまま起動 → デモモード。実送信しません。**

---

## 3. セットアップと起動

```bash
cd /path/to/openqarp
source /path/to/venv/bin/activate
export QARP_SKIP_ABI_CHECK=1

python -m pip install -r examples/community_mina/line_bridge/requirements.txt

export LINE_DEMO_MODE=true
python examples/community_mina/line_bridge/webhook_app.py
```

起動時にデモ店舗（招待コード `DEMO01`）が自動作成されます。

### スモーク（デモモード）

```bash
# ① 登録
curl -s -X POST http://127.0.0.1:8080/demo/message \
  -H 'Content-Type: application/json' \
  -d '{"text":"登録 DEMO01","userId":"Ustaff001"}' | python -m json.tool

# ② 希望休
curl -s -X POST http://127.0.0.1:8080/demo/message \
  -H 'Content-Type: application/json' \
  -d '{"text":"希望休 日曜","userId":"Ustaff001"}' | python -m json.tool

# ③ シフト見せて
curl -s -X POST http://127.0.0.1:8080/demo/message \
  -H 'Content-Type: application/json' \
  -d '{"text":"シフト見せて","userId":"Ustaff001"}' | python -m json.tool

# 店舗一覧（userId マスク）
curl -s http://127.0.0.1:8080/stores | python -m json.tool
```

webhook 形式:

```bash
curl -s -X POST http://127.0.0.1:8080/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [{
      "type": "message",
      "replyToken": "demo-token",
      "source": {"type": "user", "userId": "Ustaff002"},
      "message": {"type": "text", "text": "登録 DEMO01"}
    }]
  }' | python -m json.tool
```

プッシュ／ブロードキャスト生成のみ:

```bash
python examples/community_mina/line_bridge/notify.py --demo --flex
```

---

## 4. 対応テキスト（意図）

| ユーザー発話例 | 動作 |
|----------------|------|
| `登録 DEMO01` / `登録 店舗コード MINA01` | 店舗に userId を紐付け |
| `シフト見せて` / `シフト表` | **所属店舗**のたたき台を Flex で返信 |
| `希望休 日曜` / `希望休 A 土` | **所属店舗**のシナリオに希望休を反映して再組表 |
| `ヘルプ` | 使い方 |

未登録のまま希望休／シフトを送ると、登録を促すメッセージを返します。

本体の最適化は **古典ソルバ** です。量子比較はこの LINE 層では送りません。

---

## 5. ファイル

| ファイル | 内容 |
|----------|------|
| `stores.py` | 店舗レジストリ（JSON）。作成・招待・登録・マスク表示 |
| `stores.example.json` | 空のレジストリ雛形 |
| `webhook_app.py` | Flask webhook。登録／希望休／組表・デモ API |
| `notify.py` | push / reply / **broadcast_to_store** |
| `shift_messages.py` | 意図パース・組表・テキスト／Flex・接続ステータス |
| `config.example.env` | 環境変数テンプレ |
| `requirements.txt` | flask, requests |

Streamlit の「店舗向け」「LINE連携」は `../pitch/app.py` を参照。

---

## 6. 注意（必ず読む）

- **ライブ LINE 動作を資格情報なしで主張しません。** デモは営業説明用です。  
- **販売時は顧客の LINE 公式を使う。開発者個人 LINE は不要。**  
- Channel secret / token / userId / `stores.json` をリポジトリにコミットしないでください。  
- 署名検証は secret 設定時のみ必須。デモ時はスキップして受理します。  
- PoC のため 1 userId = 1 店舗。労働法規・本格シフト SaaS の代替ではありません。
