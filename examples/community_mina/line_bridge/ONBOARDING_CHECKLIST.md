# 店舗オーナー向け 導入チェックリスト（LINE + シフト PoC）

**所要時間の目安:** 初めてでも概ね **20〜30 分**（LINE 公式が既にある場合は **10〜15 分**）。  
デモ画面のチェックだけなら **5 分**程度。

**前提（正直に）**

| 必要 | 不要 |
|------|------|
| **お客様（店舗）の LINE 公式アカウント** | 開発者個人の LINE アカウント |
| Messaging API の Channel secret / 長期トークン | スタッフを開発者が手登録すること |
| HTTPS で届く Webhook URL（ngrok 等で可・PoC） | 本格シフト SaaS の全機能 |

Credit: Powered by OpenQARP

詳細手順・技術メモは同フォルダの [README.md](./README.md)。  
今週お試し課金までの最短は [../pitch/go_live.md](../pitch/go_live.md)。

---

## 全体の流れ（一目）

```
① LINE 公式を用意 → ② Messaging API → ③ Webhook URL
→ ④ 環境変数 → ⑤ Streamlit で店舗作成 → ⑥ スタッフ招待
→ ⑦ 登録／希望休／シフト見せて を試験 → ⑧ 本番前チェック
```

Streamlit 画面の **「導入チェックリスト」** でも同じ項目をチェックできます（進捗％付き）。

---

## Step 1. LINE 公式アカウントを用意する（約 5〜10 分）

- [ ] [LINE Official Account Manager](https://manager.line.biz/) で **店舗の公式アカウント** を作成（または既存を使う）
- [ ] アカウント名がスタッフに分かる表示名になっている（例: 「○○カフェシフト」）
- [ ] **無料プランでも Messaging API は利用可**（配信通数には上限あり。PoC・少人数なら足りることが多い）

> 開発者の個人 LINE で運用しないでください。販売・導入は **店舗オーナー側の公式** が前提です。

---

## Step 2. Messaging API チャネルを作る（約 5 分）

- [ ] [LINE Developers](https://developers.line.biz/console/) に **店舗側アカウント** でログイン
- [ ] プロバイダーを作成（または既存を選択）
- [ ] **Messaging API** チャネルを新規作成し、公式アカウントと紐付ける
- [ ] **Channel secret** を控える（チャネル基本設定）
- [ ] **チャネルアクセストークン（長期）** を発行して控える
- [ ] 「応答メッセージ」「あいさつメッセージ」は **オフ推奨**（Bot の返信と二重になるため）

秘密情報はチャットや Git に貼らない。`.env` や Streamlit secrets のみ。

---

## Step 3. Webhook URL を公開する（約 5〜10 分・PoC）

- [ ] ブリッジを起動できるマシン／サーバを用意する  
  `LINE_DEMO_MODE=false` で `python examples/community_mina/line_bridge/webhook_app.py`（既定ポート 8080）
- [ ] HTTPS で外から届く URL を用意する（PoC なら ngrok 等で可）

```bash
ngrok http 8080
# 例: https://xxxx.ngrok-free.app/webhook を LINE コンソールへ貼る
```

- [ ] LINE Developers → Messaging API → Webhook URL に `https://…/webhook` を設定
- [ ] **Webhook の利用** をオン
- [ ] 「検証」ボタンが成功する（失敗時は下の「よくある失敗」へ）

本番では固定ドメイン＋常時起動が望ましい。ngrok はデモ／お試し週向け。

---

## Step 4. 環境変数を入れる（約 3〜5 分）

- [ ] `config.example.env` をコピーして `.env` を作る

```bash
cd examples/community_mina/line_bridge
cp config.example.env .env
# 編集: LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN
set -a && source .env && set +a
```

- [ ] `LINE_CHANNEL_SECRET` = Step 2 の secret
- [ ] `LINE_CHANNEL_ACCESS_TOKEN` = Step 2 の長期トークン
- [ ] `LINE_DEMO_MODE=false`（実送信するとき）
- [ ] （任意）`LINE_INVITE_BASE_URL` = 公式の友だち追加 URL
- [ ] `.env` / `stores.json` を Git にコミットしない

未設定のまま起動すると **デモモード**（実 LINE 非送信）。営業画面共有だけならそれで十分なことも多い。

---

## Step 5. Streamlit で店舗を作成する（約 2〜3 分）

- [ ] `streamlit run examples/community_mina/pitch/app.py` を起動
- [ ] 下の方 **「店舗向け（マルチテナント登録）」** で店舗名を入れ「店舗を作成」
- [ ] **招待コード**（例: `MINA01`）を控える
- [ ] 招待リンク（QR プレースホルダ）を確認。本番は公式の友だち追加 URL に差し替え

デモ用コード `DEMO01` も自動用意されます。お試し課金店では **店名付きの専用コード** を推奨。

---

## Step 6. スタッフを招待する（約 3〜5 分）

- [ ] 公式アカウントの **友だち追加用 QR／リンク** をスタッフに送る
- [ ] 「友だち追加したあと、トークで次を送ってください」と案内する:

```text
登録 MINA01
```

（`MINA01` は Step 5 の招待コードに置き換え）

- [ ] Streamlit「店舗向け」で **登録メンバー（マスク）** が増えたことを確認
- [ ] 店長自身も一度登録しておくと、後の試験が楽

---

## Step 7. 動作試験（約 5 分）— 必須3発話

スタッフ（または店長）の LINE から、公式アカウントへ:

| # | 送る文 | 期待する動き |
|---|--------|--------------|
| 1 | `登録 <店舗コード>` | 「○○に登録しました」系の返信 |
| 2 | `希望休 日曜` | 希望休を反映した組表の返信（未登録なら登録を促す） |
| 3 | `シフト見せて` | その店舗の週次たたき台（テキスト／Flex） |

- [ ] ① 登録 OK
- [ ] ② 希望休 OK
- [ ] ③ シフト見せて OK
- [ ] （任意）Streamlit「店舗向け」でブロードキャスト **プレビュー**（強制デモON推奨）

資格情報なしのローカル確認は README の `/demo/message` curl でも可。

---

## Step 8. 本番前（Go-live）チェック

- [ ] Webhook 検証が成功したまま／サーバが落ちていない
- [ ] `LINE_DEMO_MODE` が意図どおり（本番送信なら `false`）
- [ ] 応答・あいさつメッセージが二重になっていない
- [ ] スタッフ 1〜2 名で Step 7 を再確認した
- [ ] 「これはたたき台。店長確認前提。労働法規の自動判定ではない」と口頭で伝えた
- [ ] お試し範囲（期間・人数・サポート窓口）を紙かメールで合意した → [go_live.md](../pitch/go_live.md)

---

## よくある失敗と対処

| 症状 | よくある原因 | 対処 |
|------|--------------|------|
| Webhook 検証失敗 | URL 誤り・HTTP・サーバ未起動・パスが `/webhook` でない | ngrok 先が生きているか、末尾 `/webhook`、HTTPS を確認 |
| 何も返信がない | 応答メッセージがオンで Bot と競合／Webhook オフ | 応答・あいさつをオフ、Webhook 利用オン |
| 「店舗コードが見つかりません」 | コードの全角・小文字・別店舗 | 大文字英数字で再送。Streamlit のコードと一致確認 |
| 希望休／シフトが「登録して」と返る | 未登録、または別公式に送っている | まず `登録 <コード>`。正しい公式トークか確認 |
| デモのまま実送信されない | `LINE_DEMO_MODE=true` または token 未設定 | `.env` を入れ直し、デモを off にして再起動 |
| 署名エラーで 401 | secret 不一致 | Developers の Channel secret を再コピー |
| スタッフが増えない | webhook が別マシンの古い `stores.json` を見ている | `LINE_STORES_PATH` と起動ディレクトリを統一 |
| Flex が崩れる／届かない | 端末・版の差 | テキスト形式で再試験。PoC ではテキストでも可 |

---

## 営業デモでチェックリストを使うとき

1. Streamlit の **「導入チェックリスト」** を開き、進捗バーを見せる  
2. 「御店の LINE 公式が必要。開発者個人 LINE は不要」と先に言う  
3. 既に公式がある店なら Step 2〜7 を画面共有で一緒に辿る（お試し週の核）  
4. 詳細トークは [../pitch/SELLER_SCRIPT.md](../pitch/SELLER_SCRIPT.md)、今週課金の型は [../pitch/go_live.md](../pitch/go_live.md)

**言わないこと:** 「今すぐ御店の LINE に繋がっている」「資格情報なしでライブ送信できる」「既存シフト SaaS の置き換え」
