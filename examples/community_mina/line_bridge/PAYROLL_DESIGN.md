# 給与計算・見込み管理 — 設計メモ（PoC）

## スコープ
- **やる**: 予定／実績に基づく給与計算・見込み・月次ロック・CSV・3案の総人件費スコア
- **やらない**: 銀行振込・口座・支払実行

## スタッフ拡張フィールド
| フィールド | 型 | 既定 | 意味 |
|------------|-----|------|------|
| hourly_wage | int | 1100 | 基本時給 |
| night_hourly_wage | int\|null | null→基本×深夜割増 | 深夜時給 |
| overtime_hourly_wage | int\|null | null→基本×1.25 | 残業時給 |
| commute_allowance | int | 0 | 交通費（出勤1日あたり） |
| allowances | list | [] | `{name, amount, type: monthly\|per_shift}` |

## 店舗
| フィールド | 意味 |
|------------|------|
| labor_budget_monthly | 今月の人件費予算（円） |
| payroll_months[YYYY-MM] | ロック済み給与データ |
| actual_hours_by_month[YYYY-MM][worker_id] | 実績時間（編集可・再計算可） |

## 計算式（classical_payroll_poc）
```
regular = max(0, actual_hours - night_hours - ot_hours)
base_pay     = hourly_wage × regular
night_pay    = night_hourly_wage × night_hours
ot_pay       = overtime_hourly_wage × ot_hours
commute      = commute_allowance × work_days
allowance    = Σ monthly + Σ (per_shift × work_days)
total        = base_pay + night_pay + ot_pay + commute + allowance
```
計算根拠（formula / rates / hours）を JSON に永続化し、実績編集後に再計算可能。

## シフト3案の人件費スコア
日単位ソルバ向けシミュレーション（給与確定ではない）:
- 日コスト = 時給×hps×(土日/祝日割増) + 交通費
- 深夜見込 = 週末シフトに `night_hours_per_weekend_shift`（既定2h）を付与
- 週残業見込 = max(0, 週合計時間 − 40) × (残業時給 − 基本)
最適化スコアはこの総人件費を使用（基本時給のみではない）。

## 月次見込み
確定シフト（週次）をテンプレに、月の週数（既定 4.345）でスケール。
実績未入力時は予定＝実績として見込み表示。

## LINE コマンド
- `給与 今月` / `給与 太郎` / `給与確定 9月` / `給与明細 太郎 9月`
- `人件費予算 200000` / `給与CSV 9月`
- `交通費 太郎 500` / `手当 太郎 役職手当 5000` / `深夜時給` / `残業時給`
- `実績 太郎 80` / `実績 太郎 深夜8 残業4`（実績時間編集）
