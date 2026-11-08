# OKF v0.2 LLM-Wiki（CPU/エッジ向け）

EPO データを取り込みながら、Markdown + YAML frontmatter だけで動く
知識グラフ／検索基盤です。**Pattern A（〜500ノード、外部依存ゼロ）** と
**Pattern B（SQLite-FTS5、501ノード〜）** と、任意インフラで動く
**Pattern C（ONNX / Qdrant + Gemma embeddings）** を実装しています。

- CLI: `okf`
- 実装: `src/pat_okf_agent/wiki/`
- デモ: `examples/wiki/`（合成特許データ + Workflow/Gotcha/Synergy）
- テスト: `tests/test_okf_*.py`（オフライン）
- Python: **3.12以上**（CI 3.12/3.13/3.14、推奨3.14）

## 1. 設計原則

| 原則 | 実装 |
| --- | --- |
| 1トピック = 1ファイル | `*.md` を概念ノードとして扱う |
| 最小限の frontmatter | 必須は `type` のみ。`title`/`description`/`tags`/`timestamp`/`resource` を推奨 |
| 関係は相対リンク | `[表示名](../path/to.md)` が有向エッジ。外部 URL・画像・コード内リンクは除外 |
| 段階的開示 | ルート `index.md` → セクション `index.md` → 個別ノード。上限超過は `index-2.md` へ分割 |
| 追記ログ | `log.md` に取り込み履歴を残す |
| 可搬性 | プレーンテキストのみ。ベクトル DB・GPU・独自 SDK 不要 |

### ディレクトリ例

```text
wiki/
├── index.md                 # ルート索引（自動生成）
├── log.md                   # 取り込みログ
├── .agents/
│   ├── AGENTS.md            # エージェント向け最小ガイド（動的スキル一覧）
│   ├── task_memory.md       # タスクローカル記憶
│   └── skills/              # 必要時だけバインドする SKILL.md
├── patents/
│   ├── index.md             # セクション索引（自動生成）
│   ├── index-2.md           # 51件目以降（自動生成）
│   ├── EP9999999B1.md       # 1トピック1ファイル（合成例）
│   └── US9999999A1.md       # ファミリーのスタブ（合成例）
├── workflows/
│   └── fe-amorphous-screening.md
├── gotchas/
│   └── epo-claims-availability.md
└── synergistic/
    └── ops-eps-cross-check.md
```

## 2. クイックスタート

```bash
cd /path/to/pat-okf-agent

# 初期化（索引・ログ・エージェントガイド）
uv run okf init mywiki \
  --skills-dir .agents/skills

# スキルの発見・検証、ツール契約の確認
uv run okf skills mywiki --check --json
uv run okf schema --json

# EPO から1件取り込み（OPS 書誌/ファミリー + OPS/EPS クレーム）
uv run okf ingest-epo EP9999999B1 --wiki mywiki

# 検索・グラフ探索・文脈解決・検証
uv run okf search mywiki "amorphous alloy" --limit 5
uv run okf traverse mywiki EP9999999B1 --depth 2
uv run okf traverse mywiki workflows/fe-amorphous-screening --scope gotchas
uv run okf context mywiki "アモルファス クレーム" --max-nodes 5
uv run okf search mywiki "metallic glass" --expand --json
uv run okf eval-synonyms mywiki --check
uv run okf evolve mywiki --history .agents/flow_history.jsonl
uv run okf evolve mywiki --history .agents/flow_history.jsonl --propose
uv run okf sqlite mywiki          # Pattern B を明示同期
uv run okf validate mywiki

# 索引の再生成 / CI での鮮度チェック
uv run okf index mywiki
uv run okf index mywiki --check   # 古ければ exit 1

# MCP stdio サーバ（許可ルート配下の wiki だけを操作）
uv run okf-mcp --root .

# MCP HTTP（loopback 既定。外部 bind は bearer token 必須）
OKF_MCP_TOKEN=change-me uv run okf-mcp --root . \
  --transport http --port 8765
```

`okf` は `uv run` なしでも、インストール済み環境ならそのまま使えます。

## 3. frontmatter 仕様

```yaml
---
type: Patent                 # 必須（空文字不可）
title: "ALLOY COMPOSITION..." # 推奨（無ければ最初の H1 → ファイル名）
description: "..."           # 推奨（索引・検索に使用）
resource: https://...        # 推奨（原典 URL）
tags: [epo, patent, ep]      # 推奨（文字列のリスト）
timestamp: 2026-10-29T00:00:00Z
scope: taxonomy              # taxonomy | workflow | synergistic | gotchas
publication: EP9999999B1     # 任意（公報番号の直接参照）
basis: external              # 任意（推奨）: external | model | mixed
sources:                     # 任意: 出典リスト
  - "patent-biblio:EP9999999B1 via=epo-ops"
  - "patent-claims:EP9999999B1 via=epo-ops"
verified_at: "2026-10-29"    # 任意: 照合確認日（YYYY-MM-DD）
---
```

### プロベナンス（出典・検証状態）仕様

- **`basis`**: ノート記述内容の派生元を表す認識論的フラグ。
  - `external`: 外部出典のみで裏付けられている（stub を除く全ノートで `sources` または external ノードへの相対リンク、および `verified_at` が必要）。
  - `model`: LLM の一般知識のみで書かれている。
  - `mixed`: 外部出典とモデルの一般知識が混在している（`sources` または external ノードへの相対リンクが必要）。
- **`sources`**: 出典文字列のリスト。
  - 文法: `<kind>:<id>[ issued=YYYY-MM-DD][ retrieved=YYYY-MM-DD][ via=<channel>]`（空白区切り）
  - 属性キー: `issued`（発行日）、`retrieved`（取得日）、`via`（取得経路。例: `epo-ops`, `epo-eps`）
  - `kind` 語彙（13種）と陳腐化プロファイル:
    - 不変層: `patent-biblio`（特許書誌）, `patent-claims`（特許クレーム）, `paper`（査読論文）, `standard`（標準・規格）
    - 版管理層: `datasheet`（データシート）, `product-page`（製品ページ）, `patent-legal`（法的状態）
    - 揮発層（3年経過で P8 警告）: `press-release`（プレスリリース）, `news`（報道記事）, `preprint`（プレプリント）
    - その他: `patent-family`（ファミリー情報・スタブ用）, `web`（一般Web）, `manual`（人手入力）
    - Web 系（`product-page`, `press-release`, `news`, `web`）は `retrieved=YYYY-MM-DD` が必須（P6）。
- **`verified_at`**: 本文が出典と矛盾しないことを照合した日（YYYY-MM-DD）。

### 本文の統制語彙（3対）

本文の H2 スコープ見出し（Taxonomy / Workflow / Synergistic / Gotchas 等）の内部で、根拠の性質を表す統制語彙は次の3対に固定します。

| 役割 | 日本語（正） | 英語（正） | 配置・挙動 |
|---|---|---|---|
| 未検証ブロック | `一般知識（未検証）` | `General Knowledge (Unverified)` | GFM Alert `> [!NOTE] 一般知識（未検証）` または H2〜H6 見出し。配下の単位つき数値は P4 免除 |
| 公報リンク導入 | `代表公報（実データ）` | `Representative Patents (Verified)` | `## Workflow / Implementation` 内の段落ラベル（太字推奨） |
| 出典整理節 | `出典に基づく整理` | `Based on Sources` | H3 見出し（任意）。認識論的修飾子として透過処理され親見出しを維持 |

- 表の一部の列だけが未検証数値の場合は、列ヘッダに `（一般知識・未検証）` または `(Unverified)` を記載することで、その列の数値を P4 検査から免除します。

### Concept スケルトン（標準テンプレート）

<!-- okf-concept-skeleton:start -->
```markdown
---
type: Concept
title: "窒化アルミニウム（AlN）フィラー"
description: "高熱伝導・電気絶縁性を兼ね備える窒化物セラミックスフィラー"
tags: [material, filler, nitride]
scope: taxonomy
basis: mixed
sources:
  - "patent-biblio:EP9999999B1 via=epo-ops"
---
# 窒化アルミニウム（AlN）フィラー

窒化アルミニウム（AlN）は、半導体封止材や放熱基板に用いられる代表的な高熱伝導・電気絶縁フィラーである。マトリックス樹脂との複合化設計については [EP9999999B1](../patents/EP9999999B1.md) を参照。

## Taxonomy

ウルツ鉱型結晶構造を持つ共有結合性セラミックスであり、フォノン伝導によって高い熱伝導性を示す。

> [!NOTE] 一般知識（未検証）
> 焼結体・単結晶の典型的な熱伝導率は約 150–230 W/m·K、線膨張係数（CTE）は約 4.5 ppm/K とされる（目安値、出典未照合）。

## Workflow / Implementation

高充填化では粒径分布の制御と表面処理（シランカップリング剤等）を組み合わせる。

**代表公報（実データ）:** [EP9999999B1](../patents/EP9999999B1.md)

## Synergistic Combinations

球状アルミナや窒化ホウ素（BN）との異種フィラー併用により、充填率と粘度特性のバランスを改善できる。関連手法は [EP9999999B1](../patents/EP9999999B1.md) を参照。

## Gotchas / Anti-Pitfalls

水分との反応（加水分解）によりアンモニアを生じ、界面熱抵抗の上昇や絶縁低下を招くため、耐湿処理の有無を確認する。
```
<!-- okf-concept-skeleton:end -->

- パーサは **依存ゼロ** の YAML サブセット（スカラー・インラインリスト・
  ブロックリスト）。ネストしたマッピングは誤解釈を避けるためエラーにします。
- `scope` 省略時は `type`/`tags` から推定:
  `Gotcha` → gotchas、`Workflow`/`Procedure` → workflow、
  `Synergy`/`Synergistic` → synergistic、それ以外 → taxonomy。
  これは**ノードのフォールバック**で、探索時のエッジ重みはリンクを含む
  標準見出し（`# Gotchas / Anti-Pitfalls` 等）が優先されます。
- `type` が無い、`tags` がリストでない、`timestamp` が ISO-8601 でない、
  `scope` が未知、は `okf validate` のエラーになります。

### リンク方針（相対 Markdown リンク）

本 Wiki の正本リンクは **GitHub Markdown 方式の相対リンク**です。

```markdown
[Fe基アモルファス合金の絞り込み手順](../workflows/fe-amorphous-screening.md)
[EPOクレーム取得の落とし穴](../gotchas/epo-claims-availability.md)
```

- `[[Note]]` / `[[Note|表示名]]` / `![[embed]]` は採用しません。
- `[[...]]` はグラフエッジにならず、`okf validate` が警告を出します
  （コードブロック・インラインコード内は除外）。
- パスで一意に解決できるため、同名特許・表記ゆれ・LLM のリンク生成に対して
  決定的です。GitHub / VS Code / Obsidian のいずれでも表示できます。
- 表示名は自由に付けられます。リンク先の変更時はパスを更新してください。

## 4. グラフ探索・関連度・Gotchas フィルタ

相対リンクを有向エッジとして読み込み、**リンクが書かれている見出し
セクション**の重みで探索します。

### 4.1 見出しスコープとスコア

```text
S(N_i → N_j) = Σ_e W(scope(e)) × Relevance(Q, text(e))
score(child) = score(parent) × S × γ
```

- `scope(e)` はリンクを含む見出しから判定します。
  `# Taxonomy` → taxonomy、`# Workflow / Implementation` → workflow、
  `# Synergistic Combinations` → synergistic、
  `# Gotchas / Anti-Pitfalls` → gotchas。
- 標準見出し以外（`## 関連` など）では、後方互換のため**リンク先ノードの
  scope** にフォールバックします。新規ノードは標準見出しを推奨します。
- `text(e)` はアンカーテキスト + 見出し + リンク行。
  `Relevance = |Tokens(Q) ∩ Tokens(text(e))| / |Tokens(Q)|`。
- `--query` を渡すと関連度が効きます。アンカーテキストが汎用的でも
  構造を辿れるよう、関連度の下限は既定 `0.05`（`--min-relevance 0` で厳密式）。
- `γ`（damping）は既定 `0.85`。スコアは経路の積なので、深いノードほど減衰します。
- 各ノードは一度だけ確定するため、リンク循環でも停止します。

| scope | 重み | 意味 |
| --- | --- | --- |
| taxonomy | 1.0 | 用語・分類・通常の知識 |
| workflow | 1.2 | 手順・実務フロー |
| synergistic | 1.5 | 組み合わせで価値が出る知識 |
| gotchas | 2.0 | 失敗・落とし穴（優先的に拾う） |

```bash
uv run okf traverse mywiki workflows/fe-amorphous-screening --depth 2
uv run okf traverse mywiki workflows/fe-amorphous-screening --depth 2 \
  --query "アモルファス クレーム"
uv run okf traverse mywiki workflows/fe-amorphous-screening --scope gotchas
# 1. gotchas/... (score=1.700, depth=1)
# 2. workflows/... (score=1.000, depth=0)
```

`--direction out|in|both` で出リンク／被リンク／双方向を選択できます。

### 4.2 ContextResolver（検索の意味的代替）

`okf context` は次の順で、決定論的に文脈を組み立てます。

1. 検索バックエンドで起点ノードを最大3件取得。
2. 見出しスコープ重み + クエリ関連度でグラフを展開。
3. 各候補の `Gotchas / Anti-Pitfalls` を読み、タスク制約に反するノードを枝切り。
4. スコア順に最大 `--max-nodes` 件を、トークン上限内で連結。

```bash
uv run okf context mywiki "アモルファス クレーム" --max-nodes 5
uv run okf context mywiki "アモルファス クレーム" --json
uv run okf context mywiki "アモルファス クレーム" --phases
```

制約マーカー:

- `INCOMPATIBLE_EXPLICIT` — 無条件に候補から除外。
- `INCOMPATIBLE_WITH: windows, gpu` — クエリに該当語が含まれる場合に除外。
- `NOT_FOR: production` — 同上。

段階的開示のトークン上限は Phase 1 `< 1,000`、Phase 2 `< 2,500`、
Phase 3 `< 4,000`（`--phases` で各フェーズを出力）。`--backend sqlite` を
指定すると Pattern B の FTS5 を起点検索に使います。

## 5. 段階的開示（index）

- ルート `index.md`: セクション一覧と件数、直下ノードのみ。
- セクション `index.md`: タイトル・説明・相対リンク。最大 `--max-entries`
  （既定 50）件で `index-2.md`, `index-3.md` … に分割。
- 生成物には `<!-- generated by okf index -->` が入ります。手編集せず、
  `okf index` で再生成してください。
- `okf index --check` は生成物の欠落・差分を検出し、CI やコミット前の
  検証に使えます（差分があれば exit 1）。

## 6. 検証（validate）

`okf validate` は 2 パスで構造とプロベナンスを検査します。

### 検査レベルと終了コード

| 検査種別 | 対象・ルール | 既定動作 | `--strict-provenance` |
|---|---|---|---|
| **構文・構造エラー (`validate_meta`)** | `type` 欠落、不正型、不正日付、未知の `scope`/`basis`、`sources` 文法違反、リンク切れ、Wiki ルート脱出 | エラー（exit 1） | エラー（exit 1） |
| **自己矛盾ルール (`P2`, `P4`, `P5`)** | P2（external/mixed なのに sources も外部リンクも無し）、P4（未検証マーカー外のブロック局所裏付け欠落）、P5（external なのに未検証マーカー含有） | 警告（exit 0） | **エラー昇格（exit 1）** |
| **品質・時点警告 (`P1`, `P3`, `P6`, `P7`, `P8`)** | P1（basis 未指定）、P3（external なのに verified_at 無し）、P6（Web 系 sources の retrieved 欠落）、P7（未知の source kind）、P8（揮発性ニュース出典の3年経過）、孤立ノード、index.md 欠落 | 警告（exit 0） | 警告（exit 0） |

### P4 ブロック局所検査の仕組みと限界

- **仕組み**: 段落・箇条書き・表の各ブロック内で、未検証マーカー（Alert や未検証見出し、未検証列）の外にある単位つき数値（`180 W/m·K` 等）に対し、同一ブロック内に実在する external ノードへの相対リンク、外部リンク、または sources 記載 ID のいずれかの裏付けがあるかを検査します。公報番号（`EP9999999B1`）、日付、バージョン番号、インラインコード・コードブロック内の数値は誤検知防止のため除外されます。
- **限界**: ブロック局所検査により、ノート内の別セクションに置かれた無関係なリンクでリード文の数値を誤って免除する問題は防げます。ただし「同一ブロック内に無関係な裏付けリンクがある場合」の取り違え（claim 単位の厳密な照合）は検出できず、運用上のレビューで補完します。

終了コードはエラー有りで `1`、警告のみなら `0` です。
`--json` で機械可読なレポートを出力します。

## 7. Pattern A / B / C

`okf stats` がノード数から推奨 Pattern を返します（`pattern_for`）。

| Pattern | 目安 | 実装 |
| --- | --- | --- |
| **A（既定）** | 〜500ノード | Markdown スキャン + ripgrep 起点 + リンクグラフ。外部依存ゼロ |
| **B** | 501〜2000ノード | SQLite FTS5（BM25）+ `okf_edges`（heading_scope / anchor_text）。Markdown は正本 |
| **C** | 2000超、または同義語失敗率 >15% | ONNX INT8 seed index、または Qdrant + HTTP/Gemma embeddings。任意インフラで、未設定時は `BackendUnavailable` |

### Pattern B を使う

```bash
# 明示的に同期（既定 DB: <wiki>/.okf/index.sqlite3）
uv run okf sqlite mywiki
uv run okf sqlite mywiki --db /tmp/wiki.sqlite3 --force

# FTS5 検索 / 自動選択（>500ノードで B）
uv run okf search mywiki "amorphous alloy" --backend sqlite
uv run okf search mywiki "amorphous alloy" --backend auto

# 起点検索も B
uv run okf context mywiki "アモルファス" --backend sqlite
```

- `okf files` の SHA-1 と mtime/size を比較し、変更ファイルだけ再索引。
- `okf_edges(source_path, target_path, heading_scope, anchor_text, heading, context)`。
- FTS5 は BM25 の列重み（title 10 / description 4 / tags 3 / body 1）で順位付け。
- `.okf/` は派生インデックスなので Git 管理外（`.gitignore` 済み）。
- 日本語の部分一致のため FTS5 は `trigram` トークナイザを使います
  （利用不可の SQLite では `unicode61` にフォールバック）。1〜2文字の語は
  trigram で索引できないため、ヒット無しのときだけ Python 検索へ戻します。
  既存 DB のトークナイザを変更した場合は `.okf/` を削除して再構築します。

### Pattern C を使う（任意）

Pattern C は**明示的に `--backend qdrant` を指定したときだけ**使います。
`auto` は従来どおり 500 ノード超で B を選びます。Markdown が正本で、
Qdrant / 埋め込み索引は破棄可能な派生データです。

#### 1) 埋め込みエンドポイント

`HttpEmbedder` は OpenAI 互換 (`/v1/embeddings`) と Gemini 形式の両方を
扱えます。`GemmaEmbedder` は Gemini 形式のプリセットです。ローカルの
推論サーバーでもホスト型 API でも構いません。

```bash
export OKF_EMBEDDING_URL=http://localhost:8080/v1/embeddings
export OKF_EMBEDDING_MODEL=gemma-embedding-2
# 必要な場合のみ
# export OKF_EMBEDDING_API_KEY=...
# export OKF_EMBEDDING_STYLE=openai   # or gemini

export QDRANT_URL=http://localhost:6333
# export QDRANT_COLLECTION=mywiki
# export QDRANT_API_KEY=...
```

#### 2) Qdrant へ同期して検索

```bash
uv run okf qdrant mywiki
uv run okf search mywiki "アモルファス合金" --backend qdrant --limit 5
uv run okf context mywiki "アモルファス クレーム" --backend qdrant
```

- `okf qdrant` は Markdown の SHA-1 と payload を比較し、変更分だけ
  upsert、削除分だけ delete します。`--force` で collection を作り直します。
- collection 名は既定で wiki の絶対パスから決まる `okf_<hash>` です。
- Qdrant REST を直接使うため `qdrant-client` は不要です。
- 次元が既存 collection と異なる場合は、黙って混ぜず `ValueError` で停止します。

Gemini 形式の例:

```bash
uv run okf qdrant mywiki \
  --embedding-url 'https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent' \
  --embedding-model gemma-embedding-2 \
  --embedding-api-key "$GOOGLE_API_KEY" \
  --embedding-style gemini
```

ONNX Runtime を使う場合は、`OnnxEmbedder(model_path, tokenizer)` を
`MicroOnnxBackend` または `QdrantBackend(embedder=...)` に渡します。
`onnxruntime` が無い環境では `BackendUnavailable` になります。

### 同義語展開と Pattern C 判定

`<wiki>/synonyms.json` に同義語グループを置きます。グループは2語以上で、
同じ語を複数グループに重複させないでください。

```json
{
  "groups": [
    ["amorphous alloy", "metallic glass", "アモルファス合金"],
    ["claim", "クレーム"]
  ]
}
```

検索・文脈解決で展開を有効にします。展開は上限付きで、元のクエリを必ず
先頭に残します。

```bash
uv run okf search mywiki "metallic glass" --expand --json
uv run okf context mywiki "metallic glass" --expand
# 別ファイルを使う場合
uv run okf search mywiki "metallic glass" --expand --synonyms ./aliases.json
```

`eval_queries.json` に期待ノードを書くと、展開前後の失敗率を比較できます。

```json
{
  "queries": [
    {"id": "syn-1", "query": "metallic glass", "expected": ["patents/EP1.md"]},
    {"id": "syn-2", "query": "クレーム", "expected": ["workflows/claims.md"]}
  ]
}
```

```bash
uv run okf eval-synonyms mywiki --json
uv run okf eval-synonyms mywiki --check   # 失敗率 >15% で exit 1
```

失敗率は「期待ノードが上位 `--limit` 件に入らなかったクエリ数 / 全クエリ数」
です。`--check` は CI 用で、失敗率が `--threshold`（既定 0.15）を超えると
exit 1。推奨 Pattern が C でも、バックエンドは自動では切り替えません。

### 可逆な検索アダプタ

```python
from pat_okf_agent.wiki.search import get_backend

backend = get_backend("local", "mywiki")       # Pattern A
backend = get_backend("sqlite", "mywiki")      # Pattern B
# backend = get_backend("qdrant", "mywiki",    # Pattern C（embedder 必須）
#                       embedder=my_embedder)
hits = backend.search("amorphous", limit=5)
```

`SearchBackend.search(query, limit=...) -> list[SearchHit]` を満たせば、
独自の Gemma/Qdrant 実装も差し込めます。Markdown が常に正本なので、
外部索引を破棄しても無損失でローカルへ戻せます。

`MicroOnnxBackend` は INT8 ベクトルを `.okf/embeddings/vectors.bin` に
保存し、検索時だけ mmap します。`OnnxEmbedder` は `onnxruntime` 導入時のみ
使え、未導入なら `BackendUnavailable` です。得られた seed path は
Pattern A/B と同じ決定論的グラフ巡回へ渡します。

## 8. エージェントハーネス

`okf init` は `.agents/` に最小ハーネスを生成します。

```text
.agents/
├── AGENTS.md          # 常時ロードする最小ルール（約1画面）
├── task_memory.md     # タスクローカルの durable notes
├── rules/             # 必要時だけ読むドメインルール
└── skills/
    ├── README.md
    └── <name>/SKILL.md
```

`AGENTS.md` の要点:

- frontmatter（`name` / `version` / `description`）と
  `<!-- NO_AUTO_MUTATE -->`、`<!-- LEARNED -->` センチネル。
- Phase 1〜3 のトークン上限と `okf context` の利用。
- 全スキルを常時ロードせず、`trigger_conditions` に一致する
  `SKILL.md` だけを動的バインド。
- `create-skill` / `modify-skill` メタスキル。
- `INCOMPATIBLE_EXPLICIT` などの Gotchas 制約を尊重。
- `okf init` の再実行時は `<!-- LEARNED -->` 以降を保持。

`--skills-dir` を渡すと `*/SKILL.md` の frontmatter を読み、
**利用可能スキル表（名前・トリガー・説明・場所）を動的生成**します。
未指定時は `.agents/skills` と `.opencode/skills` を探索します。
`--skills-dir` は繰り返し指定でき、明示した場合は祖先探索を行いません。
同名スキルは先に指定したディレクトリが優先されます。

### 8.1 スキル発見と検証

```bash
# 明示ディレクトリを検証（frontmatter・trigger_conditions 必須）
uv run okf skills . --skills-dir .agents/skills --check --json

# wiki からの祖先探索（.agents/skills → .opencode/skills）
uv run okf skills examples/wiki --json
```

JSON 契約:

```json
{
  "ok": true,
  "skills": [
    {
      "name": "OKF Wiki Operations",
      "description": "...",
      "triggers": "okf, 知識ベース, リンク切れ",
      "path": ".agents/skills/okf-wiki/SKILL.md"
    }
  ],
  "missing": []
}
```

- `--check` は `name` / `description` / 非空の `trigger_conditions` が
  欠けたスキル、または frontmatter が壊れたスキルがあると exit 1。
- 壊れたスキルは黙って無視せず、`error` 付きで一覧に出す。
- `trigger_conditions` はインラインリストでもブロックリストでもよい。
- リポジトリ管理下の `.agents/skills/` を正本とし、個人用の
  `.opencode/skills/` は明示的に追加バインドする。

### 8.2 MCP とツール契約

`okf schema --json` は CLI と MCP で共有する**安定した JSON ツール契約**を
出力します。`okf-mcp` は stdio の newline-delimited JSON-RPC 2.0 と、
任意の HTTP transport を提供します。

```bash
uv run okf schema --json
uv run okf-mcp --root .          # stdio
OKF_MCP_TOKEN="$(openssl rand -hex 24)" \
  uv run okf-mcp --root . \
    --transport http --host 127.0.0.1 --port 8765
```

| ツール | 用途 | 変更 |
| --- | --- | --- |
| `okf_search` | 検索シード（Qdrant/同義語展開も選択可） | なし |
| `okf_context` | トークン上限付き文脈解決（同上） | なし |
| `okf_traverse` | 重み付きグラフ探索 | なし |
| `okf_validate` | 構造・リンク検証 | なし |
| `okf_stats` | 件数・Pattern 判定 | なし |
| `okf_index` | 索引の検査／再生成 | あり |
| `okf_ingest_epo` | EPO 取り込み | あり |

MCP クライアント設定例（OpenCode など）:

```json
{
  "mcp": {
    "okf": {
      "type": "local",
      "command": ["uv", "run", "--project", "/absolute/path/to/pat-okf-agent",
                  "okf-mcp", "--root", "/absolute/path/to/pat-okf-agent"]
    }
  }
}
```

#### HTTP transport

- `POST /mcp`: 1メッセージの JSON-RPC。`Content-Type: application/json`。
  `Accept: text/event-stream` の場合は単一の SSE `event: message` を返します。
- `GET /health`: 認証不要の liveness probe。
- notification（`id` なし）は `202 Accepted`、本文なし。
- `DELETE /mcp`: 204。セッション状態は持たないため実質的な終了通知です。
- `/mcp` は `Authorization: Bearer <token>` で保護できます。トークンは
  `--token` または `OKF_MCP_TOKEN`。loopback 以外へ bind する場合は
  トークン必須です。
- `--port 0` で空きポートを選び、実際の URL を stderr に表示します。

```bash
curl -s http://127.0.0.1:8765/health
curl -s -H "Authorization: Bearer $OKF_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' \
  http://127.0.0.1:8765/mcp
```

- `--root` は許可ルート。`wiki` 引数は解決後にこの配下でなければ拒否。
- `okf_ingest_epo` の `offline` は既定 `true`。ネットワーク取得は
  EPO スキルがキャッシュ欠損を確認した後だけ `false` にする。
- `okf_search` / `okf_context` は `backend=qdrant` と
  `embedding_url` / `qdrant_url` / `embedding_style` などを受け取れます。
  秘匿値は引数より `QDRANT_API_KEY` / `OKF_EMBEDDING_API_KEY` の
  環境変数を推奨します。`expand_synonyms=true` は
  `<wiki>/synonyms.json` を使います。
- ツール失敗は JSON-RPC 自体を壊さず `isError: true` の content で返す。
- HTTP は単発 SSE のみで、複数セッションの双方向 SSE、OAuth、監査ログ、
  レート制限は未実装です。外部公開時は TLS 終端・認証・レート制限を
  リバースプロキシ側で追加してください。

### 8.3 EvolutionLoop（決定論的ベースライン + 提案）

`okf evolve` は追記型の `flow_history.jsonl` から繰り返し現れる手順を
N-gram クラスタリングし、レビュー可能な `SKILL.md` 草案を生成します。
LLM による自動改変は行いません。既存スキルへの反映は、生成された提案を
人間が確認してから行います。

```json
{"task": "EPO 文献を取り込む", "steps": ["epo check", "okf ingest-epo", "okf validate"], "outcome": "success"}
{"task": "EPO 文献を取り込む", "steps": ["epo check", "okf ingest-epo", "okf validate"], "outcome": "success"}
```

```bash
uv run okf evolve mywiki --history .agents/flow_history.jsonl --dry-run
uv run okf evolve mywiki --history .agents/flow_history.jsonl --apply
uv run okf evolve mywiki --history .agents/flow_history.jsonl --propose
uv run okf evolve mywiki --history .agents/flow_history.jsonl \
  --propose --force
uv run okf evolve mywiki --history .agents/flow_history.jsonl \
  --apply --learn
```

- 既定は dry run（`--dry-run` で明示可）。`--apply` を付けたときだけ
  `.agents/skills/<slug>/SKILL.md` を書きます。`--dry-run` と `--apply` は
  同時指定できません。
- `--propose` は既存スキルを変更せず、レビュー用の提案を
  `.agents/skills/.proposals/<slug>.md` に書きます。既存スキルがある場合は
  観測手順と既存手順を並べ、追加・削除・未変更の差分、レビュー
  チェックリスト、候補草案を示します。既存スキルがない場合は新規
  スキルの草案を示します。`.proposals` には `SKILL.md` がないため、
  `okf skills` の探索対象にはなりません。
- 既存の提案ファイルは上書きしません。レビュー中に追記したメモを守るため、
  更新には明示的な `--force` が必要です。`--force` は `--apply` または
  `--propose` と併用した場合だけ有効です。
- `--propose` と `--dry-run` は同時指定できません。`--apply` と併用すると、
  新規スキルの作成と既存スキルの提案を同時に行えます。
- 既存スキルは上書きしません。明示的な `--force` がある場合だけ
  `--apply` で置換します。
- 同じタスクから複数のクラスタが採れた場合は、slug の衝突を避けるため
  `-2`, `-3` のような一意な接尾辞を付けます。自動上書きはしません。
- `--learn` は `--apply` 必須で、`AGENTS.md` の
  `<!-- LEARNED -->` 以降に短い要約を追記します。
- `--min-support`（既定 2）、`--ngram`（既定 3）、`--max-clusters` で
  感度を調整できます。生成物は必ずレビューし、古い手順は修正してください。
- 履歴ファイル自体は追記専用の運用データです。秘匿情報や巨大な
  ツール出力を含めないでください。

### 8.4 メタスキル

`create-skill` / `modify-skill` は、最寄りの `.agents/skills/` 配下の
`SKILL.md` を読んで使います。スキルを追加・変更したら
`okf skills --check` と、必要に応じて `okf init` による `AGENTS.md` 再生成を
行ってください。`<!-- LEARNED -->` 以降の追記は保持されます。

## 9. EPO 取り込み

### 単件

```bash
uv run okf ingest-epo EP9999999B1 --wiki mywiki
```

- `biblio`（書誌）と `family`（INPADOC ファミリー）は OPS から取得。
- クレームは OPS fulltext を優先し、**404/空なら EPS（認証不要の公式 EP
  文書）へ自動フォールバック**します（EP 文献のみ）。
- ファミリーメンバーは `patents/<PUB>.md` のスタブとして作成し、
  相互リンクを作ります。後でそのメンバーを取り込むと**スタブから昇格**し、
  親を再取り込みしてもスタブに戻りません。
- 書き込みは `.tmp` + `os.replace` の原子更新。同じ入力の再実行で
  ノード内容は変わりません（`log.md` には履歴が1行増えます）。
- `index.md` は取り込み後に自動再生成されます。

### 検索して一括

```bash
uv run okf ingest-epo --query 'ti=amorphous and pa="EXAMPLE"' \
  --wiki mywiki --max-results 10
```

OPS Free プランの検索上限（25件）を超える指定は自動的に 25 に丸めます。

### オプション

| オプション | 動作 |
| --- | --- |
| `--offline` | ディスクキャッシュのみ使用（追加のネットワーク呼び出しなし） |
| `--no-family` | ファミリー取得・スタブ作成を行わない |
| `--no-claims` | クレーム取得を行わない（EPS フォールバックも無効） |
| `--no-stubs` | ファミリーは本文に**リンクなしのテキスト**で列挙（リンク切れ防止） |
| `--json` | 取得件数・クレーム数・スタブ・警告を JSON で出力 |

Fair use は `epo.py` 側で維持されます（OPS 3秒間隔・クロスプロセス、
検索25件上限、ディスクキャッシュ優先）。1件の取り込みは最大4リクエスト
（biblio / claims / family / EPSフォールバック）で、以降はキャッシュで完結します。

## 10. 出典検証（verify-source）

特許（EPO）以外の学術論文（DOI / arXiv）やメーカー公式データシート・Web ページ（URL / 層4論文）を、決定論的かつキャッシュファーストで機械検証し、客観的証跡を保存します。

### CLI コマンド例

```bash
# DOI の検証（Crossref → JaLC → doi.org CSL フォールバック）
uv run okf verify-source 10.1111/jace.14748 \
  --expect-title "Aluminum Nitride" \
  --check-claim "180 W/m" \
  --json

# arXiv ID の検証
uv run okf verify-source arXiv:2401.12345 --expect-author "Smith" --json

# Web 出典（データシート・製品ページ等）の検証とスナップショット保存
uv run okf verify-source "datasheet:https://example.com/aln-datasheet.pdf" \
  --issued 2024-05-01 \
  --json
```

### 検証フローとフォールバック
- **Crossref DOI**: Crossref Works API で書誌（タイトル、著者、出版者、完全日付、JATS XML 除去済み abstract、撤回フラグ）を照合。
- **JaLC / DataCite フォールバック**: Crossref 404 時、`doi.org/ra` で登録機関を判定。JaLC は JaLC REST API、DataCite 等は `doi.org` CSL-JSON content negotiation でメタデータを解決。
- **arXiv ID**: `export.arxiv.org` の Atom XML から書誌・abstract を抽出。出版版 DOI（`<arxiv:doi>`）があれば警告とメタデータに記録。
- **Web URL**: `data/external/source_verify/snapshots/` に本文 `<key>.<ext>` と `.meta.json` サイドカー（`sha256`, `extracted_publisher`, `extracted_issued`, `retrieved_at`）をアトミック保存。PDF は `pypdf`（`--extra pdf`）でテキスト抽出し `--check-claim` を検証。

### 終了コード契約
- `0` (`verified`): 実在確認および期待条件（`--expect-*`, `--check-claim`）の照合成功。`okf_source` 文字列を生成。
- `1` (`not_found` / `mismatch` / `unverified`): 出典不在、書誌不一致、ペイウォール（HTTP 403）、または主張文字列の不一致。
- `2` (`error`): 引数不正、API 障害、HTTP 429 リトライ超過、オフライン時のキャッシュミス。

### 機構とポリシーの分離
ツールは客観的証跡（出版者、発行日、ハッシュ、スナップショット、文字列一致結果）のみを返し、信頼スコアや一次/二次情報の判定は行いません。返却された証跡に基づき、エージェントがノート本文のブロック単位 grounding と注記を決定します。

## 11. デモ

`examples/wiki/` は**合成特許データ**（`EP9999999B1` と架空のファミリー）に、
Workflow / Gotcha / Synergistic / 制約付き Gotcha を加えた実例です。
実在のEPOデータは含みません。
標準見出し（Taxonomy / Workflow / Gotchas / Synergistic）でエッジ重みを
付けてあります。

```bash
uv run okf stats examples/wiki          # 34 concepts / 69 links / Pattern A
uv run okf validate examples/wiki
uv run okf traverse examples/wiki workflows/fe-amorphous-screening --depth 2
uv run okf traverse examples/wiki workflows/fe-amorphous-screening --scope gotchas
uv run okf context examples/wiki "アモルファス クレーム" --max-nodes 5
# -> ライセンス制約ノードは "pruned by Gotchas" として除外される
uv run okf sqlite examples/wiki --db /tmp/okf-demo.sqlite3
uv run okf search examples/wiki "クレーム" --backend sqlite \
  --db /tmp/okf-demo.sqlite3
```

## 12. 制約と次の一手

- Pattern A/B/C は実装済みです。C は `--backend qdrant` の明示指定でのみ
  有効で、`auto` は 500 ノード超で B を選びます。ONNX Runtime や
  Qdrant / 埋め込みサーバーは任意インフラで、未設定時は
  `BackendUnavailable` になります。
- ONNX/Qdrant アダプタは fake transport に加え、in-process HTTP server を
  使った実 `urllib` 経路の統合テストでも検証済みです。実際の Qdrant
  クラスタ、Gemma エンドポイント、ONNX モデルファイルはこのリポジトリに
  同梱していないため、導入先で接続確認してください。
- 同義語展開と失敗率評価を実装しました。`synonyms.json` と
  `eval_queries.json` を用意し、`okf eval-synonyms --check` で CI に
  組み込めます。失敗率 >15% は Pattern C の推奨材料ですが、自動移行は
  しません。
- Maestro EvolutionLoop は決定論的ベースラインと提案モードを実装しました。
  `flow_history.jsonl` の N-gram クラスタリング、草案生成、明示的な
  `--apply`、`--propose` による非破壊の更新提案、`<!-- LEARNED -->` 追記
  までです。LLM による SkillMutator / 自動マージは未実装で、提案も含めて
  生成物は必ず人間がレビューします。
- MCP は stdio と HTTP の両方を提供します。HTTP は単発 SSE、
  Bearer token、`/health`、loopback 既定で、複数セッション SSE・OAuth・
  監査ログ・レート制限は未実装です。`okf_ingest_epo` は既定 offline の
  ため、フェアユースを破る経路にはなりません。
- EPO 以外のソース（USPTO）は既存の `uspto` クライアントで取得し、
  `type`/`scope` を付けた Markdown を生成すれば同じグラフに載ります。
  USPTO → OKF の自動インジェスタは未接続です（今回の対象外）。
- 大規模化したら `okf stats` と `okf eval-synonyms` の推奨を目安に、
  B/C へ明示的に移行してください。

## 13. リリース前チェック

リリース候補では、テスト・デモ検証・スキル検証・オフラインスモーク・
秘密情報チェックをまとめて実行します。

```bash
uv sync --locked
uv run python -m unittest discover -s tests -q
uv run okf validate examples/wiki
uv run okf index examples/wiki --check
uv run okf skills . --skills-dir .agents/skills --check --json
uv run python scripts/release_smoke.py
uv build --out-dir /tmp/okf-dist
uv run python scripts/check_dist.py /tmp/okf-dist
```

詳細なGate、CI、ロールバック、既知の制約は
[`docs/release_checklist.md`](release_checklist.md) を参照してください。
