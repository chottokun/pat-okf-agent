---
type: Concept
title: OKFの段階的開示
description: index.md から必要なセクションだけを辿り、文脈を最小化してCPU/エッジでも扱えるようにする設計
tags: [okf, taxonomy, progressive-disclosure]
timestamp: 2026-10-29T00:00:00Z
scope: taxonomy
---
# OKFの段階的開示

## Taxonomy

- `index.md` はセクションへのリンクだけを持ち、全文を載せない。
- 各セクションの `index.md` は最大50件（`--max-entries`）で分割し、`index-2.md` 以降へ送る。
- エージェントは検索 → セクション索引 → 個別ノードの順に読み、必要になるまで本文を開かない。
- ノード間の関係は相対リンクで表し、リンクグラフをそのまま探索に使う。

## Workflow / Implementation

- 実務への適用例: [Fe基アモルファス合金の絞り込み手順](../workflows/fe-amorphous-screening.md)

## Gotchas / Anti-Pitfalls

- 取得時の注意: [EPOクレーム取得の落とし穴](../gotchas/epo-claims-availability.md)
- 本文を先に全部読むと、Phase 3 の 4,000 トークン上限を超えやすい。
