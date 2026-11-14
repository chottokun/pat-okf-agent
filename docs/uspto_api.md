# USPTO 特許データ取得（Open Data Portal API）

USPTO の特許（出願・特許ファイルラッパー）データを ODP API 経由で取得する共有
モジュールです。`epo.py` と同じ「キャッシュファースト・低頻度・検証付き」の設計です。

- 実装: `src/okf_kb_agent/uspto.py`
- テスト: `tests/test_uspto_api.py`（11件・オフライン）
- エンドポイント: `https://api.uspto.gov/api/v1`（Patent File Wrapper）

## 0. 前提（2026年時点の重要事項）

- **PatentsView は廃止済み**（旧 `search.patentsview.org` は 2026 年に停止、データは ODP に移行）。
  古い記事のコードをそのまま使わないこと。
- **ODP API は無料だが API キー必須**。`USPTO.gov` アカウント（MFA 必須、2026-08-18 から
  プロフィール4項目の記入必須）で <https://data.uspto.gov/apikey> から取得する。
  キー無しの要求は `401 {"message":"Unauthorized"}`。
- キーが無効化される条件（プロフィール未記入など）があるため、401 が返ったら
  ODP の「Manage API Key」で再アクティベートする。
- バルクデータ製品のダウンロードはキー不要だが、**API 利用にはキーが必要**。

## 1. 認証情報

`.secrets/uspto_credentials.json`（`.gitignore` 済み。コミット禁止）:

```json
{
  "api_key": "YOUR_ODP_API_KEY",
  "endpoint": "https://api.uspto.gov/api/v1",
  "note": "DO NOT COMMIT"
}
```

環境変数でも可:

```bash
export USPTO_API_KEY=...
# 任意
export USPTO_API_ENDPOINT=https://api.uspto.gov/api/v1
export USPTO_CREDENTIALS=/path/to/uspto_credentials.json
export USPTO_RATE_STATE=/path/to/uspto_rate_state.json   # "" でプロセス間制御を無効化
```

## 2. ライブラリ利用

```python
from okf_kb_agent.uspto import UsptoClient, parse_data_bag

client = UsptoClient()
res = client.search("applicationMetaData.inventionTitle:graphene", limit=10)
print(res["count"])
for rec in parse_data_bag(res):
    print(rec["applicationNumberText"])

meta = client.metadata("18999999")     # 出願メタデータ
cont = client.continuity("18999999")   # 継続出願
txns = client.transactions("18999999")
```

### メソッド一覧

| メソッド | 用途 | エンドポイント |
|---|---|---|
| `search(q, limit=25, start=0, ...)` | 出願検索（q構文・日付範囲） | `/patent/applications/search` |
| `application(n)` | 出願1件 | `/patent/applications/{n}` |
| `metadata(n)` | 書誌メタデータ | `.../meta-data` |
| `continuity(n)` | 継続関係 | `.../continuity` |
| `transactions(n)` | 経過履歴 | `.../transactions` |
| `documents(n)` | 書類一覧 | `.../documents` |
| `assignment(n)` | 譲渡 | `.../assignment` |
| `attorney(n)` | 代理人 | `.../attorney` |
| `foreign_priority(n)` | 外国優先権 | `.../foreign-priority` |
| `adjustment(n)` | 特許期間調整（PTA） | `.../adjustment` |
| `status_codes()` | ステータスコード表 | `/patent/status-codes` |

出願番号は `18/999,999` などの表記を受け付け、内部で数字のみに正規化します。

## 3. CLI

```bash
uv run uspto check                 # キー解決のみ（ネットワークなし）
uv run uspto search 'graphene' --limit 10
uv run uspto app 18999999
uv run uspto metadata 18999999
uv run uspto continuity 18999999 > continuity.json
uv run uspto --offline search 'graphene' --limit 10   # キャッシュのみ
```

## 4. フェアユース

- 要求間隔は最低 **1 秒**。`data/external/uspto_rate_state.json` に最終要求時刻を
  flock 排他制御で記録し、**プロセス間でも共有**（`USPTO_RATE_STATE=""` /
  `--no-rate-state` で無効化）。
- 検索の結果窓は **1〜25 件**に上限化（`MAX_RESULTS`）。ページングは `start` を使う。
- 成功レスポンスは `data/external/uspto_raw/` に raw JSON で保存。再実行は
  キャッシュから返り、サーバー負荷ゼロ。`--refresh` で明示的に再取得。
- 429 / 5xx は `Retry-After` 尊重＋指数バックオフ（最大2回）。
- 401 はキー無効。メッセージに再発行手順を表示する。
- 失敗レスポンスは `*.error.txt` に保存し、成功キャッシュとして扱わない。

## 5. ダウンロードと解析（バルクデータ / 個別 XML）

USPTO の XML は「ダウンロード → ローカル解析」が基本です。ODP のデータ製品
ファイル（`/api/v1/datasets/products/files/...`）は **API キーが必要**です
（実測: キー無しで 401）。公開 URL はキー無しでもダウンロードできます。

```bash
# 汎用ダウンロード（.part 経由・Content-Length 検証・429/5xx リトライ）
uv run uspto download "https://api.uspto.gov/api/v1/datasets/products/files/PTGRXML-SPLT/2024/ipg240604/18999999_12000000.xml" -o data/external/uspto_raw/

# 週次 ZIP を安全に展開（絶対パス・..・シンボリックリンクを拒否）
uv run uspto extract ipg240604.zip -o data/external/uspto_raw/ipg240604/

# 1 文献の解析（JSON: publication_number / title / abstract / inventors / assignees / claim_count）
uv run uspto parse data/external/uspto_raw/18999999_12000000.xml --claims

# 巨大な週次 XML を逐次処理（1 行 1 文献の JSON）
uv run uspto parse ipg240604.xml --stream

# 手動ダウンロードした週次 ZIP をそのまま解析（キー不要・展開不要）
uv run uspto parse ipg240604.zip --claims
```

ライブラリ:

```python
from okf_kb_agent.uspto_bulk import (
    product_file_url, download, extract_zip, parse_grant_xml, iter_grant_documents,
)
url = product_file_url("PTGRXML-SPLT", 2024, "ipg240604", "18999999_12000000.xml")
download(url, "out.xml", api_key=...)          # 公開 URL なら api_key 不要
doc = parse_grant_xml("out.xml")               # claims を含む dict
for doc in iter_grant_documents("ipg240604.xml"):
    print(doc["publication_number"], doc["claim_count"])
```

- XML 形式は USPTO の `us-patent-grant`（DTD v4.5 系）。`iter_grant_documents` は
  名前空間・DTD の有無に依存せず、`us-patent-grant` 要素単位でストリーム処理します。
- 週次バルクは数百 MB 級になり得るため、`.part` 残骸の有無・`bytes` 表示・
  `claim_count > 0` を確認してから「解析成功」と判断してください。
- 米国のクレーム全文はこの XML から取得します（EPO OPS の claims のような軽量 JSON は無い）。
- ZIP は展開前に十分な空き容量を確認してください。

## 6. 年単位の一括ダウンロード

ODP のバルク製品には「週次ファイルの集まり」と「年・10年単位のパッケージ」が混在します
（例: `PTGRXML` = 週次 ZIP、`PTFWPRE` = 10年単位 ZIP）。**「年単位」は製品のファイル一覧
API に期間を渡して、その年の全ファイルを列挙 → 順次ダウンロード**で実現します。

```bash
# 製品を探す（キー必須）
uv run uspto products 'PTGRXML'

# 2024年のファイル一覧だけ確認（ダウンロードなし・キー必須）
uv run uspto product PTGRXML --year 2024

# 2024年分を一括ダウンロード（まず --dry-run で計画確認）
uv run uspto year PTGRXML 2024 -o data/external/uspto/2024 --dry-run
uv run uspto year PTGRXML 2024 -o data/external/uspto/2024
```

ライブラリ:

```python
from okf_kb_agent.uspto import UsptoClient
from okf_kb_agent.uspto_bulk import product_files, download_year

client = UsptoClient()
files = product_files(client, "PTGRXML", year=2024)      # 期間指定で列挙
summary = download_year(client, "PTGRXML", 2024, "data/external/uspto/2024")
print(summary["total_files"], summary["total_bytes"])
```

- 一覧は `fileDataFromDate`/`fileDataToDate`（その年の 1/1〜12/31）で絞り込み、
  `fileDataBag` をページングして取得します（`includeFiles=true`）。
- ダウンロードは**再実行安全**: 同名・同サイズの既存ファイルは `skipped` になり、
  中断後の再実行で続きから取得できます（`ok` / `skipped` / `planned` を確認）。
- 週次フルテキスト 1 年分は**数十 GB** になり得ます。空き容量を確認し、まず
  `--dry-run` の `total_files` と各 `fileSize` を見積もってください。
- `year` は「その年に発行されたファイル」を対象にします。会計年度や出願年ではなく
  **ファイルのデータ期間**基準です。

## 7. 注意点

- ODP の検索構文・フィールド名は ODP のドキュメントに従う（`q` パラメータ）。
- 米国出願の**全文・クレーム**は ODP の Patent File Wrapper では XML ダウンロード
  （`grantDocumentMetaData.fileLocationURI`）経由になる。EPO OPS の claims API の
  ような単純な JSON クレームは提供されない。
- USPTO データは公式レジスタの代替ではなく、法的助言ではない。
