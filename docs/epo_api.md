# EPO 特許 API 共通アクセス層

EPO の特許データ取得を 1 か所に集約した共有モジュールです。個別の反復スクリプトに
OAuth 処理やキャッシュ処理をコピーせず、ここを経由してください。

- 実装: `src/pat_okf_agent/epo.py`
- テスト: `tests/test_epo_api.py`
- 対象サービス:
  - **EPO OPS 3.2** — 書誌検索・biblio・family・fulltext/claims・legal・EP Register（要 OPS アカウント）
  - **EPO European Publication Server (EPS) REST v1.2** — 公式 EP A/B 文献の XML（認証不要）

## 1. 認証情報の準備

OPS のキーは各自が取得し、**リポジトリにコミットしない**でください。
`.secrets/` は `.gitignore` 済みです。

`.secrets/ops_credentials.json`:

```json
{
  "consumer_key": "YOUR_CONSUMER_KEY",
  "consumer_secret": "YOUR_CONSUMER_SECRET",
  "token_url": "https://auth.epo.org/oauth/token",
  "api_root": "https://ops.epo.org/3.2/rest-services",
  "note": "DO NOT COMMIT"
}
```

環境変数でも上書きできます（CI や一時利用向け）:

```bash
export EPO_OPS_CONSUMER_KEY=...
export EPO_OPS_CONSUMER_SECRET=...
# 任意
export EPO_OPS_TOKEN_URL=https://ops.epo.org/3.2/auth/accesstoken
export EPO_OPS_API_ROOT=https://ops.epo.org/3.2/rest-services
export EPO_OPS_CREDENTIALS=/path/to/ops_credentials.json
```

`token_url` が `https://auth.epo.org/oauth/token` の環境では名前解決できない場合が
あります。その場合、クライアントは自動的に従来の
`https://ops.epo.org/3.2/auth/accesstoken` を 2 番目に試します。
トークンは `.secrets/ops_token_cache.json` に 20 分弱キャッシュされ、自動更新されます。
キャッシュにはキーの短いハッシュだけを保存し、キー本文は保存しません。

### 他プロジェクトでの再利用

- `epo.py` は標準ライブラリのみに依存する単一ファイルで、ドメイン固有のロジックは含みません。
  任意のプロジェクトにコピーして使えます（`User-Agent` は `EPO_OPS_USER_AGENT` で上書き可能）。
- 既定のキャッシュ/シークレット配置はモジュール位置相対（`<project>/.secrets`、
  `<project>/data/external/*_raw`）ですが、コンストラクタ引数
  （`credentials_path` / `token_cache` / `cache_dir`）または環境変数で完全に上書きできます。

## 2. ライブラリとして使う

```python
from pat_okf_agent.epo import OpsClient, EpsClient

ops = OpsClient()                      # .secrets/ と環境変数を自動解決
res = ops.search('pa="EXAMPLE CORP" and ti="alloy"', end=25)
print(res.total_count)
for pub in res.publications:
    print(pub.docdb)                   # 例: WO.9999999.A1

biblio = ops.biblio("WO9999999A1")
print(biblio["title"], biblio["applicants"])

family = ops.family("WO9999999A1")
claims = ops.claims("WO9999999A1")  # OPS fulltext の claims

eps = EpsClient()                      # 認証不要
official = eps.info("EP9999999B1")     # EPS XML の metadata + claims
print(official["date_publ"], len(official["claims"]))
```

公開番号の表記は `EP9999999B1` / `EP.9999999.B1` / `EP 9999999 B1` を受け付けます。
内部では `EP.9999999.B1` に正規化します（`normalize_docdb`）。

### 主なメソッド

| メソッド | 用途 | 主な OPS エンドポイント |
|---|---|---|
| `OpsClient.search(cql, begin, end)` | CQL 書誌検索 | `/published-data/search` |
| `OpsClient.biblio(pub)` | 書誌・出願人・発明者・優先権 | `/published-data/publication/docdb/{docdb}/biblio` |
| `OpsClient.family(pub)` | INPADOC ファミリー | `/published-data/family/publication/docdb/{docdb}` |
| `OpsClient.fulltext(pub, section=...)` | fulltext / claims / description | `/published-data/publication/docdb/{docdb}/...` |
| `OpsClient.claims(pub)` | クレーム全文 | 上記 `section="claims"` |
| `OpsClient.legal(pub)` | 法的状態 | `/legal/publication/docdb/{docdb}` |
| `OpsClient.register_search(q)` | EP Register 検索 | `/register/search` |
| `OpsClient.register(pub, section=...)` | Register 詳細 | `/register/patent/{epodoc}/{section}` |
| `EpsClient.document(pub, kind=...)` | 公式 EP XML | `data.epo.org/publication-server/rest/v1.2/...` |
| `EpsClient.claims(pub, kind=...)` | 公式クレーム | 同上 |
| `EpsClient.info(pub, kind=...)` | 公式メタデータ + クレーム | 同上 |

OPS の Register 詳細（`biblio` / `events` など）は Free プランのキー範囲外で 404 に
なることがあります。その場合は生のエラーを `.error.txt` に残して例外を返します。
「取得できない＝権利不存在」と解釈しないでください。

## 3. CLI

```bash
# 認証情報の解決だけ確認（ネットワークなし）
python -m pat_okf_agent.epo check

# トークンを 1 回だけ取得（必要な場合のみ）
python -m pat_okf_agent.epo token

# 検索（OPS の検索結果は最大 25 件が Free プランの実用窓）
python -m pat_okf_agent.epo search 'pa="EXAMPLE INDUSTRY"' --end 25

# 書誌 / ファミリー / 法的状態
python -m pat_okf_agent.epo biblio WO9999999A1
python -m pat_okf_agent.epo family WO9999999A1
python -m pat_okf_agent.epo legal US9999999A1

# 公式 EP 文献（EPS、認証不要）
python -m pat_okf_agent.epo eps-info EP9999999B1
python -m pat_okf_agent.epo eps-claims EP9999999B1
python -m pat_okf_agent.epo eps-document EP9999999B1 > /tmp/EP9999999B1.xml

# 共通オプションはサブコマンドより前に置く
python -m pat_okf_agent.epo --offline biblio WO9999999A1
python -m pat_okf_agent.epo --refresh search 'pa="EXAMPLE INDUSTRY"'
```

`--offline` はキャッシュのみを使用します。`--refresh` はキャッシュを無視して再取得します。
`--cache-dir` / `--token-cache` / `--sleep` も指定できます。

## 4. フェアユースとキャッシュ（Free プラン配慮）

- OPS へのネットワーク呼び出しは **最低 3 秒間隔**（`RateLimiter`）です。
- 間隔は **プロセス間でも共有** されます: 最終要求時刻を
  `data/external/ops_rate_state.json`（default, `EPO_OPS_RATE_STATE` で上書き可）に
  flock 排他制御で記録するため、複数スクリプトを同時実行しても合計のアクセス頻度は
  通常どおり保たれます。無効化は環境変数 `EPO_OPS_RATE_STATE=""` または CLI の
  `--no-rate-state`（単一・逐次実行専用）。
- **検索結果の取得範囲は 1〜25 件に上限化** しています（`MAX_SEARCH_RESULTS`、
  `search()` / `register_search()` に共通）。Free プランで許容される実用窓を超える
  指定は `ValueError` になり、無駄な要求を出しません。
- 成功したレスポンスは `data/external/ops_raw/` に raw JSON として保存し、次回は
  キャッシュから返します（`refresh=True` で再取得）。
- 429 / 5xx は指数バックオフで最大 2 回再試行します（`Retry-After` ヘッダ尊重）。
- 失敗レスポンスは `*.error.txt` に保存し、成功キャッシュとして扱いません。
- EPS の XML は `data/external/eps_raw/` にキャッシュします（`.gitignore` 済み）。
- 既存の `data/external/ops_raw/*.json` はそのまま読めます。過去のエラー本文
  （XML など）が `.json` として残っている場合は、パース不能ならキャッシュミスとして
  再取得します。

## 5. OKF Wiki・MCP との連携

取得した特許データを OKF v0.2 ナレッジベース（Markdown + 相対リンクグラフ）へ取り込む場合は、
`okf ingest-epo` コマンドまたは MCP の `okf_ingest_epo` ツールを使用してください（詳細は `docs/okf_wiki.md` を参照）。
カスタムスクリプトから利用する場合は、直接 `from pat_okf_agent.epo import OpsClient, EpsClient` を import してください。

## 6. テスト

ネットワークを使わずに、トークンキャッシュ・検索・biblio・family・claims・EPS URL・
エラー処理を検証します。

```bash
python -m unittest discover -s tests -v
```

## 7. 注意点

- 本モジュールは調査用のデータ取得層であり、法的助言ではありません。
- OPS の検索結果・書誌は各国特許庁の公式レジスタの代替ではありません。
- EPS は番号指定の全文取得のみで、検索や法的状態は提供しません。
- Espacenet の非公開 REST や Patentscope の結果エンドポイントは、この環境では
  403/504 でブロックされた記録があります。公式に提供される OPS と EPS を使ってください。
- 認証情報・トークン・取得した raw データをコミットしないでください。
