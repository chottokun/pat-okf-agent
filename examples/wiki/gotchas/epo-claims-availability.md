---
type: Gotcha
title: EPOクレーム取得の落とし穴
description: OPSのfulltext/claimsは404や空応答になり得る。EPS公式文書へフォールバックして検証する
resource: https://data.epo.org/publication-server/rest/v1.2
tags: [gotcha, epo, claims, eps]
timestamp: 2026-10-29T00:00:00Z
scope: gotchas
---
# EPOクレーム取得の落とし穴

## Taxonomy

- 対象特許: [EP9999999B1](../patents/EP9999999B1.md)
- 関連サービス: OPS fulltext / EPS publication server

## Gotchas / Anti-Pitfalls

### 症状

- `epo claims EP9999999B1` が 404 を返す、または空のクレームリストになる。
- 書誌（biblio）とファミリーは取得できるため、成功したように見える。

### 原因

- OPS の fulltext は全文献・全プランで提供されるわけではない。
- Free プランでは参照できる範囲が限定される。

### 対処

1. OPS のクレームが空なら、**認証不要の EPS** を使う:
   `epo eps-claims EP9999999B1`
2. 取得したクレームが特許ノードの `## Claims` と一致するか確認する。
3. 出典（OPS / EPS）を [OPS/EPSクロスチェック](../synergistic/ops-eps-cross-check.md) に従って分けて記録する。
4. それでも取得できない場合は「未取得」と明示し、推測で補完しない。

## Synergistic Combinations

- 実務手順: [Fe基アモルファス合金の絞り込み手順](../workflows/fe-amorphous-screening.md)
