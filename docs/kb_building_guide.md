# OKF ナレッジベース（KB）構築実践ガイド

本書は、第三者が `pat-okf-agent` を用いて新しい OKF v0.2 Markdown ナレッジベース（KB）をゼロから立ち上げ、証拠に裏付けられた高品質な知識グラフを構築・拡張するための実践手順書です。

> [!TIP]
> 本ガイドと併せて、CLI の詳細仕様は [okf_wiki.md](okf_wiki.md)、設計思想の背景や落とし穴の詳細は [design_rationales.md](design_rationales.md) を参照してください。

---

## 0. 前提環境

* Python **3.12 以上**（`uv` 推奨）
* リポジトリのセットアップと動作確認：
  ```bash
  git clone https://github.com/chottokun/pat-okf-agent.git
  cd pat-okf-agent
  uv sync
  uv run python -m unittest discover tests
  ```

---

## 1. KB の初期化と型の選択

まず、KB 用のディレクトリを作成して初期化します（`.agents/skills/` をバインドすると、エージェントに OKF の作法を継承させられます）。

```bash
uv run okf init my-kb --skills-dir .agents/skills
```

構築するドメインに合わせて、以下の 3 つの代表的な型（プロファイル）から構成を決めます。

| KB の型 | 主な出典（`sources:`） | 証拠の置き場 | 特徴・向いている用途 |
|---|---|---|---|
| **特許＋Web技術調査型** | EPO/USPTO 特許、データシート、査読論文 | 共有キャッシュ（`data/external/source_verify`）または KB 内 `evidence/` | 産業技術・材料・特許調査 |
| **論文中心型** | arXiv プレプリント、学会論文（DOI） | KB 内 `evidence/arxiv/` ＋ `evidence/fulltext/` | AI/CS 研究、学術サーベイ |
| **運用知見・手順型** | 内部手順書、設定・実験ログ（`ops-record:`） | KB 内 `evidence/*.txt` ＋ `.meta.json` | チーム・システム運用ノウハウ |

> いずれの型でも、利用者手元のローカル素材（PDF・仕様メモ等）を `okf verify-source --file` で `local-file:` 出典として `<wiki>/evidence/snapshots/` に保全取り込みできます。

---

## 2. 候補選定と重複・タグ衝突の事前チェック

新しい概念ノートを書き始める前に、既存概念との重複やタグ衝突を確認します。

```bash
uv run okf candidate-check --json my-kb concept-a concept-b concept-c
```

* `name_occupied` が `null` 以外であれば、すでに同名の概念が存在するため作成を中止または既存ノートの拡充に切り替えます。
* **タグ設計のベストプラクティス**:
  * **Tier A タグ**: 概念自身の `slug` や分類用の名前空間タグ（例: `strand:inference-serving`）など、表記揺れを許さない固定語彙。
  * **Tier B タグ**: 自由記述タグ。**「検索」「エージェント」「推論」などの汎用すぎる一般カテゴリ語を単独でタグに入れないでください。** 検索エンジンはタグに高い重み（15点）を置くため、汎用タグを入れると他の既存概念の検索1位を奪う回帰の原因になります。固有の手法名・システム名を優先してください。

---

## 3. 一次情報の検証と証拠スナップショット取得（Grounding）

主張の裏付けとなる一次情報（arXiv、DOI、Web ページ等）を、`okf verify-source` で機械検証します。

```bash
# レート制限状態ファイルが KB ルートを汚さないよう作業用パスを指定
export OKF_VERIFY_RATE_STATE=/tmp/my_kb_rate_state.json

# 例: arXiv 論文の検証（KB 内部の evidence/ にスナップショット保存）
uv run okf verify-source "arXiv:2309.06180" \
  --cache-dir my-kb/evidence \
  --json

# 例: 利用者手元のローカル素材（PDF / HTML / TXT / Markdown）の取り込み（オフライン完結）
uv run okf verify-source --file ./materials/report.pdf \
  --cache-dir my-kb/evidence \
  --check-claim "180 W/m" \
  --json
```

### 確認すべきポイント
1. **Identity の確認**: `--json` 出力の `metadata.title` と `metadata.url`（バージョン付き）を確認し、意図した論文・版と一致していることを確かめます。誤った ID を指定していないかを執筆前にここで水際防止できます。
2. **`okf_source` の転記**: 出力された `okf_source` 文字列（例: `"preprint:arXiv:2309.06180 issued=2023-09-12 retrieved=2026-11-14 via=arxiv"` やローカル素材 `"local-file:report.pdf retrieved=2026-11-14 via=local-file snapshot=local_<sha256先頭16桁>"`）をそのままノートの `sources:` に使います（`--issued YYYY-MM-DD` を付けた場合のみ `issued=` が含まれ、角括弧は入りません）。
3. **ローカル素材取り込み（`--file`）の仕様と境界**:
   * `verify-source --file` が保証するのは「受け取ったバイト列の同一性（SHA-256）」であり、ファイル出所の真偽は保証しません。
   * `.md` / `.markdown` ファイルを渡した場合でも、`snapshots/` には `.txt` 拡張子で保全されるため `okf validate` に誤パースされません（元ファイル名・元拡張子は `.meta.json` に保持）。
   * 内容が同一でファイル名が異なる素材を重ねて渡した場合、内容ハッシュ `local_<sha256[:16]>` を共有し、`.meta.json` の `original_filename` には最初に取り込んだファイル名が残ります。
   * 不採用にしたローカル素材にも `uv run okf verify-source --file <path> --cache-dir my-kb/evidence --orphan-reason "<理由>"` で孤立理由を記録できます。
4. **全文スナップショットの保全（論文中心型）**: 要旨だけでなく本文の数値や限界を扱う場合は、HTML/PDF から抽出した全文 `.txt` と sha256 を記録した `.meta.json` を `my-kb/evidence/fulltext/` 等に保存してください。
   > [!CAUTION]
   > **`evidence/` 配下に `.md` ファイルを置かないでください。** `okf validate` は KB 配下のすべての `.md` を概念ノートとしてパースするため、証跡やメモの拡張子は必ず `.txt` や `.json` にしてください。

---

## 4. 概念ノートの執筆と相互リンク

`my-kb/concepts/<slug>.md` に 1 概念 = 1 ファイルで作成します。

### 必須 Frontmatter 例
```yaml
---
type: Concept
title: "概念の日本語タイトル（英語略称など）"
description: "1〜2文の簡潔な要約。"
tags: [concept-slug, strand:category, SpecificMethodName]
scope: taxonomy
basis: external
sources:
  - "preprint:arXiv:2309.06180 issued=2023-09-12 retrieved=2026-11-14 via=arxiv"
verified_at: "2026-11-14"
---
```

### 本文執筆の規律
* **数値は実験条件とセットで書く**: 「スループット 2 倍」だけでなく、「対象モデル、GPU、データセット、比較対象ベースライン」を明記します。
* **相互リンク（相対リンク）**: すでに `concepts/` に存在する概念へは `[表示名](other-slug.md)` の形式でリンクし、なぜ関係があるのかを一言添えます。まだ作成していない概念はリンクにせず、素テキストのままにします（リンク切れは `validate` でエラーになります）。
* **複数論文を扱う場合の主従明記**: 定義原典となる「主論文」を `sources` の 1 行目に置き、追随・対抗論文とは本文の役割と `## 履歴メモ` で明確に書き分けます。

---

## 5. 必須ゲート検証と検索回帰テスト

ノートを追加・更新したら、以下のゲートをすべて通して品質を保証します。

```bash
# 1. 索引（index.md）の自動生成・更新
uv run okf index my-kb

# 2. 厳格な出典・証拠チェック（--evidence-dir は必ず明示する！）
uv run okf validate my-kb \
  --strict-provenance \
  --check-evidence \
  --evidence-dir my-kb/evidence

# 3. 索引の鮮度チェックと統計確認
uv run okf index my-kb --check
uv run okf stats my-kb --json

# 4. リンク到達性・被参照 0 件概念・孤立クラスタの診断（事実確認）
uv run okf links my-kb --json
uv run okf links my-kb --from <起点概念slug>
```

### 合格基準
* `validate` が終了コード `0`、警告ゼロ、missing `0`、unexplained orphan `0` であること。
* `stats --json` で `"broken_links": 0` および `"undeclared": 0` であること。

> [!WARNING]
> `--evidence-dir` を省略すると、`okf validate` はデフォルトの `data/external/source_verify` を参照し、KB 内部の `evidence/` を持つ KB では全件 missing 扱いになります。**必ず `--evidence-dir <KB>/evidence` を明示してください。**

### リンク完全性の継続診断（`okf links`）
`okf validate` はリンク切れと「入出力ともに 0 件の完全孤立ノード」のみを検査するため、発リンクはあるが他から一切参照されていない概念（`zero_inbound`）や分断された小クラスタは見落とされます。
`okf links` は読み取り専用（常に終了コード `0`・管理ノートを除外する `--no-admin` 既定）で以下の客観的事実を返すため、各バッチ完了時に確認して相互リンクを補ってください：
* `isolated`: 入リンク・出リンクともに 0 件のノード一覧
* `zero_inbound`: 他ノートからの被参照が 0 件のノード一覧（孤立かハブ起点かを `is_isolated` で判別可能）
* `zero_outbound`: 他ノートへの発リンクが 0 件の行き止まりノード一覧
* `components`: 弱連結成分の分割一覧（`component_count > 1` なら孤立した島がある）
* `seed_reachability`: `--from <seed>` 指定時の、起点から辿れないノード一覧（`unreachable`）

### 検索品質の維持（`eval_queries.json`）
KB が 10 概念以上に育ったら、KB 直下に `eval_queries.json`（代表的な検索クエリと期待される 1 位ノートのペア）を置き、新規概念を追加するたびに `okf search my-kb "<query>" --limit 5 --json` で **既存の検索順位が落ちていないか（回帰テスト）** を確認してください。順位低下が起きた場合は、`--json` 出力の `breakdown` で原因フィールドを特定してタグや説明文を調整します。

---

## 6. 安全に KB を拡張し続ける運用コツ

1. **原本保護（コピー側で育てる）**:
   * 本番の KB（クラウドドライブや共有リポジトリ等）を直接編集せず、ローカルの作業コピー上で執筆・ゲート検証・監査を完結させてから、合格した差分だけを同期（`rclone copy` ＋ `rclone check` 等）します。
2. **小バッチ主義（1 回 2〜3 概念）**:
   * 一度に 10 件以上を追加すると、人間による数値突合や相互リンク確認が破綻します。1 回 2〜3 概念の小バッチで回し、毎回ゲートとスモークテストを通すことで品質を高く保てます。
3. **不採用スナップショットの衛生管理**:
   * `verify-source` で取得したものの結局ノートに採用しなかったスナップショットには、必ず `--orphan-reason "<理由>"` を記録してください。これにより `validate --check-evidence` での未説明孤立（`[E2]`）警告を防げます。
4. **ビューア用 JSON バンドルのエクスポート（ローカル連携時）**:
   * 構築した複数の KB をまとめてビューア向け JSON（`concepts`・`patents`・`synonyms`・件数サマリ）として出力する場合は、`scripts/export_kb_viewer_all.py`（`--base <KB親ディレクトリ> --out <出力JSONパス> [--scope all|concepts|patents]`）を使用できます（`python3` と `.venv` / `uv run python` のどちらでも動作し、`--scope patents` でも `related_concepts` 逆リンクは維持されます）。将来は本体 CLI の `okf export` へ正式化予定です。
