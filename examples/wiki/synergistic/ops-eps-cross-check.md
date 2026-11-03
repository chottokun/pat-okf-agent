---
type: Synergy
title: OPS/EPSクロスチェック
description: OPSの書誌・ファミリーとEPSの公式クレームを突き合わせ、出典を分離して信頼性を上げる
resource: https://example.invalid/patent/EP9999999B1
tags: [synergistic, epo, verification, claims]
timestamp: 2026-10-29T00:00:00Z
scope: synergistic
---
# OPS/EPSクロスチェック

単一ソースに依存せず、役割の異なる2つの EPO サービスを組み合わせる。

| 情報 | 一次ソース | 予備ソース | 確認ポイント |
| --- | --- | --- | --- |
| 書誌・出願人 | OPS biblio | Espacenet 画面 | 出願人名の表記ゆれ |
| ファミリー | OPS family | Espacenet | メンバー漏れ、kind code |
| クレーム | OPS claims | EPS 公式 XML | 請求項数、従属関係 |
| 法的状態 | OPS legal | EP Register | 最新イベント日 |

## Taxonomy

- 対象特許: [EP9999999B1](../patents/EP9999999B1.md)

## Workflow / Implementation

1. 対象特許の書誌とファミリーを OPS で取得する。
2. OPS クレームが空なら [EPOクレーム取得の落とし穴](../gotchas/epo-claims-availability.md) に従い EPS を使う。
3. クレーム番号と従属関係を照合し、相違があればノード本文に出典付きで残す。

## Synergistic Combinations

- 実務の組成スクリーニングは [Fe基アモルファス合金の絞り込み手順](../workflows/fe-amorphous-screening.md) に接続する。

## Gotchas / Anti-Pitfalls

- 取得できなかった情報は「未取得」と書き、推定で埋めない。
- 同じ内容でも出典が異なれば、どちらから取得したかを記録する。
