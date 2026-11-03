---
type: Workflow
title: Fe基アモルファス合金の絞り込み手順
description: 特許クレームの組成範囲を起点に、XRD/DSCでアモルファス形成を確認する実務ワークフロー
resource: https://example.invalid/patent/EP9999999B1
tags: [workflow, alloy, screening, epo]
timestamp: 2026-10-29T00:00:00Z
scope: workflow
---
# Fe基アモルファス合金の絞り込み手順

## Workflow / Implementation

1. [EP9999999B1](../patents/EP9999999B1.md) のクレーム1を読み、Fe-B-P-Cu の組成範囲と任意添加元素の上限を表に落とす。
2. 候補組成を 3〜5 点に絞り、真空溶解または急冷リボン法で試料を作る。
3. XRD でハローパターンを確認し、結晶ピークの有無を記録する。
4. DSC でガラス転移温度と結晶化温度を測り、ΔTx を比較する。
5. 磁気特性が必要な場合は VSM で飽和磁化と保磁力を測る。

## Gotchas / Anti-Pitfalls

- クレームの数値範囲は実施例ではなく**クレーム**を基準にする。実施例だけを見ると範囲を誤る。
- EPO OPS でクレームが取得できない場合は、[EPOクレーム取得の落とし穴](../gotchas/epo-claims-availability.md) を確認し、EPS へフォールバックする。
- ライセンス確認前の環境では [ライセンス制約で利用不可](../gotchas/license-restriction-incompatible.md) に該当しないか確認する。

## Synergistic Combinations

- 取得した書誌・クレームは [OPS/EPSクロスチェック](../synergistic/ops-eps-cross-check.md) の手順で出典を分けて記録する。
