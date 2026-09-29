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

**実務で導入するとき**は [ONBOARDING_CHECKLIST.md](./ONBOARDING_CHECKLIST.md) をそのまま使ってください（Streamlit にも同内容のチェック UI あり）。

---

## 店舗オンボーディングの流れ

詳細（共有 OA の正直な前提・隔離）: [SAAS_ONBOARDING.md](./SAAS_ONBOARDING.md)

```
店長
  │  公式を友だち追加 →「店舗作成 青山店」→ 招待コード発行（自分が店長）
  │  （Streamlit「店舗向け」でも作成可）
  │  友だち追加 URL／QR ＋ 招待コードをスタッフへ配布
  ▼
スタッフ
  │  同じ公式を友だち追加（PoC: 共有 OA）
  │  「登録 CODE」または「登録 CODE 太郎」
  │  「名前 太郎」で表示名の設定・変更も可
  ▼
日常（常に所属 store_id のみ）
  │  「希望休 日曜」→ 自分の枠に希望休を反映
  │  「シフト見せて」／「自分のシフト」
  ▼
店長（自店のみ: スタッフ・時給・給与・予算）
     「シフト3案作って」→「確定」→ 自店メンバーへ通知
     「給与 今月」「給与確定」※振込なし
```

**隔離:** A店店長は B店のスタッフ／賃金／給与を見られない。回帰: `python3 test_store_isolation.py`

## 販売版の注意事項・同意

店長が初めて店長機能を使うと、次の 2 段階で同意を記録します。

1. `注意事項`（または店長機能への初回アクセス）で注意事項・免責を表示
2. `上記を確認しました` → `同意する` の順に送信

同意は店長メンバーに `consent_at`、`terms_version`、`store_id`（内部互換用に `consent_store_id` も保存）として保存されます。規約文面を変更するときは `TERMS_VERSION` を更新してください。ドラフトは [利用規約.md](./利用規約.md) と [プライバシーポリシー.md](./プライバシーポリシー.md) です。

`給与確定` は即時ロックせず、`⚠️ この金額はシステムによる計算結果です。勤務実績・各種手当・法定割増等を確認しましたか？` を表示します。確認後に `同意する` または `確定する` を送るとロックされます。


## 店長向けコマンド（LINE 日本語）

| コマンド | 内容 |
|----------|------|
| `店舗作成 青山店` | 新規店舗＋招待コード。送信者が店長 |
| `店長登録 DEMO01` | 既存コードで店長（他店店長の横断は拒否。初回／同一店舗／オーナーコードのみ） |
| `シフト3案作って` | 希望優先／人件費優先／バランスの 3 案＋指標＋最適化前比較 |
| `今週の人件費見せて` | 週次予定人件費（通勤・深夜・残業見込込み。振込なし） |
| `人件費を下げて再計算` | 人件費寄せで再生成 |
| `人件費 120000円以内で組み直して` | 予算付き再生成 |
| `太郎さんは週20時間以内` | 週の最大勤務時間 |
| `時給 1200` / `時給 太郎 1500` | 時給（予定人件費用） |
| `割増 土日 1.25` | 土日・祝日・深夜の割増倍率 |
| `確定` / `確定 希望` / `確定 2` | 案を保存しスタッフへ通知 |
| `人件費予算 200000` | 月次人件費予算（円） |
| `交通費 太郎 500` | 出勤1日あたり交通費 |
| `深夜時給 太郎 1500` / `残業時給 太郎 1500` | 深夜・残業時給（未設定時は割増倍率から算出） |
| `手当 太郎 役職手当 5000` | 各種手当（月額。末尾に「出勤」で日額） |
| `給与 今月` | 全スタッフの月次給与見込み＋予算差額・人件費率 |
| `給与 太郎` | 個人の時間・基本・深夜/残業・交通・手当・合計 |
| `実績 太郎 80時間 深夜8 残業4` | 実績時間編集 → 実績給与再計算 |
| `給与確定 9月` | 月次実績をロック保存（**振込なし**） |
| `給与明細 太郎 9月` | 明細風表示 |
| `給与CSV 9月` | `/workspace/payroll_exports/` に CSV 出力 |

各案の指標: 希望休達成・必要人数不足日・総勤務時間・**予定人件費（通勤・深夜・残業見込込み）**・公平性。  
組表本体は **classical_greedy_heuristic**。注目日のみ QAOA Max-Cut 比較フック（週次量子最適化ではない）。  
給与計算は **classical_payroll_poc**（見込み・明細・CSVまで。銀行振込は行わない）。

スタッフ向け追加: `自分のシフト`（自分の枠のみ）。

設計メモ: [PAYROLL_DESIGN.md](./PAYROLL_DESIGN.md)

店舗データは `stores.json`（または `LINE_STORES_PATH`）に保存されます。

| フィールド | 内容 |
|------------|------|
| `store_id` | 内部 ID（例: `store_a1b2c3d4`） |
| `store_name` | 表示名 |
| `invite_code` | 招待コード（例: `DEMO01`） |
| `line_user_ids[]` | 登録済みスタッフの LINE userId |
| `preferences` | 希望休など店舗共有設定 |
| `members[].hourly_wage` | 基本時給 |
| `members[].night_hourly_wage` / `overtime_hourly_wage` | 深夜・残業時給（null なら倍率から） |
| `members[].commute_allowance` | 交通費（円/出勤日） |
| `members[].allowances[]` | 各種手当 `{name, amount, type}` |
| `members[].max_hours_week` | 週の最大勤務時間（任意） |
| `members[].available_days` | 勤務可能日（任意・None=全日） |
| `members[].role` / `skills` | 役割・スキル |
| `members[].is_manager` | 店長フラグ |
| `members[].consent_at` / `terms_version` / `store_id` | 店長の販売版同意記録 |
| `wage_premiums` | 土日・祝日・深夜の割増倍率 |
| `labor_budget_monthly` | 月次人件費予算 |
| `payroll_months` / `actual_hours_by_month` | ロック済み給与・実績時間 |
| `pending_plans` / `confirmed_plan` | 3案キャッシュと確定シフト |

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
# 要 LINE_STORES_ADMIN_TOKEN（未設定なら 403）
curl -s -H "X-Admin-Token: $LINE_STORES_ADMIN_TOKEN" http://127.0.0.1:8080/stores | python -m json.tool
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
| `payroll.py` | 給与計算・見込み・月次ロック・CSV（振込なし） |
| `plans.py` | シフト3案・総人件費シミュレーション |
| `PAYROLL_DESIGN.md` | 給与データ構造・計算式の設計メモ |
| `webhook_app.py` | Flask webhook。登録／希望休／組表／給与・デモ API |
| `notify.py` | push / reply / **broadcast_to_store** |
| `shift_messages.py` | 意図パース・組表・テキスト／Flex・接続ステータス |
| `config.example.env` | 環境変数テンプレ |
| `requirements.txt` | flask, requests |
| `ONBOARDING_CHECKLIST.md` | **店舗オーナー向け導入チェックリスト**（実務ステップ・失敗例） |

Streamlit の「導入チェックリスト」「店舗向け」「LINE連携」は `../pitch/app.py` を参照。  
今週お試し課金の最短手順は `../pitch/go_live.md`。

---

## 6. 注意（必ず読む）

- **ライブ LINE 動作を資格情報なしで主張しません。** デモは営業説明用です。  
- **販売時は顧客の LINE 公式を使う。開発者個人 LINE は不要。**  
- Channel secret / token / userId / `stores.json` をリポジトリにコミットしないでください。  
- 署名検証は secret 設定時のみ必須。デモ時はスキップして受理します。  
- PoC のため 1 userId = 1 店舗。労働法規・本格シフト SaaS の代替ではありません。
