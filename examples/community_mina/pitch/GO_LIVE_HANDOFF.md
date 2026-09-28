# shift+LINE PoC — Go-live handoff（日本語）

更新: 2026-09-29（JST）  
対象ブランチ: `community/mina-examples`

## 結論

資格情報なしの **ドライ go-live は完了**しました。LINE の実 API は一度も呼んでいません。実店舗で使うには、店舗オーナーが自分の LINE 公式アカウントで以下を順番に行ってください。

## 次にやること（初めて LINE 公式を作る場合）

1. [ ] スマートフォンのブラウザで [LINE Official Account Manager](https://manager.line.biz/) を開き、店舗用の LINE 公式アカウントを新規作成する（表示名・業種・地域を入力）。開発者個人アカウントは運用に使わない。
2. [ ] 同じ店舗アカウントで [LINE Developers Console](https://developers.line.biz/console/) にログインし、プロバイダーを作成または選択する。
3. [ ] **Messaging API チャネル**を作成し、作成した公式アカウントと接続する。
4. [ ] チャネル基本設定の **Channel secret** を控え、Messaging API 設定の **長期チャネルアクセストークン**を発行する。値はチャット・Git・スクリーンショットに貼らない。
5. [ ] リポジトリルートで環境を準備し、`config.example.env` を `.env` にコピーする。

   ```bash
   cd /path/to/openqarp
   cp examples/community_mina/line_bridge/config.example.env examples/community_mina/line_bridge/.env
   # .env に実際の secret / token を入力（このファイルはコミットしない）
   set -a && source examples/community_mina/line_bridge/.env && set +a
   export LINE_DEMO_MODE=false
   ```

6. [ ] Webhook を起動する（既定ポートは 8080）。

   ```bash
   python examples/community_mina/line_bridge/webhook_app.py
   ```

7. [ ] スマートフォンから操作できる HTTPS 公開 URL を用意する。PoC では別ターミナルで `ngrok http 8080` でもよい。LINE Developers に設定する URL は次の**置き換え前提のプレースホルダー**です。

   ```text
   https://<あなたの公開ホスト>/webhook
   ```

8. [ ] LINE Developers → Messaging API → Webhook settings で上の URL を設定し、**Webhook の利用**をオンにして「検証」を押す。応答メッセージ／あいさつメッセージは Bot と二重にならないようオフ推奨。
9. [ ] 別ターミナルで営業・店舗画面を起動する。

   ```bash
   streamlit run examples/community_mina/pitch/app.py
   ```

10. [ ] Streamlit の「店舗向け（マルチテナント登録）」で店舗名を入力して作成し、表示された招待コードを控える。
11. [ ] スタッフに公式アカウントを友だち追加してもらい、トークで `登録 <招待コード>` を送ってもらう。
12. [ ] 実 LINE で次の3発話を順番に試し、返信と店舗メンバー表示を確認する。

    ```text
    登録 <招待コード>
    希望休 日曜
    シフト見せて
    ```

13. [ ] 店長がシフトのたたき台を確認する。これは PoC であり、労働法規の自動判定・勤怠・給与・既存 SaaS の置き換えではないことを利用者に伝える。

## 環境変数（名前を変えない）

| 変数 | 実運用 | 用途 |
|---|---|---|
| `LINE_CHANNEL_SECRET` | 必須 | Webhook 署名検証 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 必須 | LINE reply / push |
| `LINE_USER_ID` | 任意 | 単一宛先 push 用 |
| `LINE_DEMO_MODE` | `false` | `true` は実送信しない強制デモ |
| `LINE_WEBHOOK_HOST` | 任意 | 既定 `0.0.0.0` |
| `LINE_WEBHOOK_PORT` | 任意 | 既定 `8080` |
| `LINE_STORES_PATH` | 任意 | 店舗 JSON。既定は `line_bridge/stores.json` |
| `LINE_INVITE_BASE_URL` | 任意 | 友だち追加 URL のベース |
| `LINE_REQUIRE_REGISTER` | 任意 | 既定 `true`。未登録の希望休／シフトを拒否 |
| `LINE_ALLOW_ORPHAN` | 任意 | ローカル実験用。実運用では通常未設定 |
| `LINE_SCENARIO_JSON` | 任意 | シフトシナリオ JSON |

## この環境で検証済み（デモ）

- `stores.create_store()` API で店舗 **DEMO practice**、招待コード **DEMO** を作成。
- `Udemo` で `登録 DEMO` → 店舗登録返信。
- `希望休 日曜` → A の日曜希望休を保存し、Flex とテキストの組表を返信（希望休 5/5）。
- `シフト見せて` → 店舗名付きの週次たたき台（テキスト + Flex）を返信。
- `/health` は `デモモード`、`/demo/message` 3 回、`/stores` は HTTP 200。実 LINE 送信は 0 件。
- 実行ログ: `/workspace/go_live_demo.log`、レスポンス記録: `/workspace/go_live_demo_responses.jsonl`。

### 重要な境界

上記はローカル Flask のデモ確認です。`Udemo` は架空の userId で、実ユーザーの検証ではありません。Channel secret、アクセストークン、Webhook の外部到達性、LINE コンソールの検証は未実施です。実運用では店舗側が発行した値だけを使ってください。

## 詳細リンク（この fork）

- [ONBOARDING_CHECKLIST.md](https://github.com/mina131225-jpg/openqarp/blob/community/mina-examples/examples/community_mina/line_bridge/ONBOARDING_CHECKLIST.md)
- [go_live.md](https://github.com/mina131225-jpg/openqarp/blob/community/mina-examples/examples/community_mina/pitch/go_live.md)
- [line_bridge README](https://github.com/mina131225-jpg/openqarp/blob/community/mina-examples/examples/community_mina/line_bridge/README.md)

**直ちに行う次の一手:** 店舗オーナーがスマートフォンで LINE 公式 → Messaging API チャネルを作り、secret/token を `.env` に入れてから、Webhook 検証と上記3発話を実 LINE で行う。
