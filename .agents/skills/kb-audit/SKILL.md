---
name: KB Audit
description: Execute deterministic promotion-gate checks, reproducible numeric-claim sampling via okf claims, and evidence-backed audit recording before promoting staging notes or closing an OKF knowledge batch.
trigger_conditions:
  - audit
  - promotion gate
  - kb audit
  - 監査
  - 昇格ゲート
  - 抜き取り照合
  - 監査記録
---

# KB Audit

## 1. 射程の原則

> 監査は正しさの保証ではなく、反証可能な誤りの排除。設計目標は「明らかな間違いを拾う検査を、どこまで安く・漏れなく回すか」。道具は証拠だけ返し、判定は返さない。

監査の目的は、反証可能な誤り（出典との矛盾・来歴の不一致・内部不整合・解決できない識別子）の排除であり、正しさの証明ではない。これを越える厚さの検証は本線にしない。

## 2. 適用場面

- staging → 本番 KB への昇格前
- バッチの締め（closing gate）
- 監査対象：新規／変更ノート ＋ KB 全体のゲート

## 3. 決定的ゲート（順序固定）

以下の順序で全て実行し、pass/fail だけでなく**生の数値**を記録する。

1. **validate + check-evidence**：
   - `uv run okf validate <wiki> --strict-provenance --check-evidence --evidence-dir <wiki>/evidence --json`
   - 対象 KB 自身の `--evidence-dir` を必ず明示する（呼び出し形式の詳細は `okf-design-conventions` に従う；共有キャッシュを使う半導体 KB のみリポジトリルートから `--evidence-dir data/external/source_verify` を指定する）。
   - 記録値：errors / warnings、`referenced` / `missing` (`[E1]`) / `orphans` (`[E2]`) 件数。
2. **index --check**：
   - `uv run okf index <wiki> --check --json`
   - 記録値：`ok`、stale 件数。
3. **links**：
   - `uv run okf links <wiki> --json`
   - 記録値：ノード数（`node_count`）・エッジ数（`edge_count`）・孤立数（`isolated_count`）・broken link 数。
4. **検索 eval**（クエリ集合がある KB のみ）：
   - `uv run okf eval-synonyms <wiki> --json`
   - 記録値：failure_rate・improved / regressed 件数。

## 4. 抜き取り照合

1. **母集団抽出とサンプリング**：
   - `uv run okf claims <wiki> --sample <N> --seed <S>` を実行し、`seed` を必ず記録する（`--note <slug>` で対象ノートに絞ることもできる）。
   - サンプル数 `N` の基準：新規／変更ノートの主張が **15 件以下なら全件**（`--sample` なし）、**それ以外は 15–20 件**。
2. **証跡との照合**：
   - 各サンプルについて、ノートの `sources` が指す引用元の証跡（`<wiki>/evidence/snapshots/`、全文、または `verify-source` キャッシュ）と照合する。
   - 結果は次の 3 値で記録する：
     - `supports`：証跡が主張の数値を裏付けている
     - `contradicts`：証跡と主張の数値・内容が矛盾している
     - `unverifiable`（理由つき）：証跡不足・未取得等で検証できない
3. **所見と grounding 規律**：
   - **所見（finding）になるのは `contradicts` のみ**。
   - 実験結果・性能数値を記述しているにもかかわらず、条件（モデル・ハードウェア・データセット・ベースライン）を伴わない数値は grounding 上の問題として記録する（paper-centric profile の規律）。

## 5. 記録の型

監査記録（Markdown）には次の項目を必ず含める。保存先は従来どおり Drive の `records`（運用手順側の指定に従う）。

- **対象 KB・ユニット**
- **構築者（Builder）** / **監査者（Auditor）** / **日付**
- **ゲート出力の数値**（validate / check-evidence / index --check / links / 検索 eval の生の件数）
- **サンプル情報**（`seed`・`N`・`total_claims`・各主張の 3 値結果と理由）
- **所見（Findings）**（`contradicts` および grounding 上の問題）
- **未確認の範囲**（`unverifiable` の内訳・対象外とした範囲）
- **昇格の可否と決めた者**

## 6. 役割と差し戻し

- **相互監査**が本線、**自己監査**も正規ルートとする。いずれの場合も構築者と監査者を必ず記録する。
- `contradicts` が 1 件でも出たユニットは昇格を止め、構築者が修正したうえで**影響範囲だけを再監査**する。

## 7. 委譲先の明示

本スキルは監査の手順だけを持ち、他スキルの内容を二重記述しない：

- ノート構造・frontmatter・`[P1]`–`[P9]` 来歴規則・indexing → `.agents/skills/okf-wiki/SKILL.md`
- DOI / arXiv / Web 出典検証・スナップショット取得 → `.agents/skills/source-verify/SKILL.md`
- 値域・鮮度・証拠配置・タグ階層・ゲート呼び出し規約 → `.agents/skills/okf-design-conventions/SKILL.md`
- 候補選定・バッチ運営・報告・Reflection → `.agents/skills/kb-batch-discipline/SKILL.md`
