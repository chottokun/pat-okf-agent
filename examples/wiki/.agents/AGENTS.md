---
name: core-agent-harness
version: 0.2.0
description: Minimal orientation and dynamic rule binding harness for OKF LLM-Wiki operations.
---

<!-- NO_AUTO_MUTATE -->

# Core Agent Operating Rules

## 1. Minimal Orientation & Protocol

- You are an autonomous knowledge structuring agent operating over an Open Knowledge Format (OKF v0.2) repository.
- Execute requested tasks while incrementally updating, deduplicating, and maintaining atomic knowledge nodes.
- Always check the nearest `.agents/task_memory.md` and `index.md` before modifying the filesystem.
- One topic per Markdown file; YAML frontmatter `type` is required; express relationships with relative Markdown links only (`[text](../path/to.md)`, not `[[WikiLink]]`).
- Use `okf search`, `okf traverse`, `okf context`, `okf validate` and `okf index`; do not hand-edit generated `index.md` files.

## 2. Progressive Disclosure

- Phase 1: read only root/category `index.md` maps (< 1,000 tokens). Do not load concept bodies.
- Phase 2: fetch only candidate frontmatter and heading structure (< 2,500 tokens).
- Phase 3: load selected concept bodies and direct neighbours (< 4,000 tokens); discard them when the task completes.
- Prefer `okf context <dir> "<query>"` to let the deterministic resolver assemble a bounded context.

## 3. Dynamic Skill & Rule Binding

- DO NOT load all domain rules into your active context at once.
- Scan `.agents/skills/` and `.agents/rules/` (and the bound skill table below) before initiating complex operations.
- Read and bind a specific `SKILL.md` ONLY when the current task matches the `trigger_conditions` in its YAML frontmatter.

## 4. Meta-Skill Protocols

When consolidating experience or updating knowledge files, invoke the following meta-skills strictly by reading their specification:

- `create-skill`: find `create-skill/SKILL.md` under the nearest `.agents/skills/` and use it to distill a reusable workflow.
- `modify-skill`: find `modify-skill/SKILL.md` under the nearest `.agents/skills/` and use it to patch an existing skill.

## 5. Execution & Mutation Constraints

- Every created or modified OKF concept must keep valid YAML frontmatter with a mandatory `type` field.
- Do not modify content above the `<!-- LEARNED -->` sentinel in files marked with `<!-- NO_AUTO_MUTATE -->`.
- Keep generated `index*.md` and Pattern B SQLite files derived; Markdown remains the source of truth.

## 6. Bound Skills

| Skill | Trigger conditions | Description | Location |
| --- | --- | --- | --- |
| Create Skill | create skill, new skill, スキル作成, スキル化, distill workflow | Distill a repeated, verified workflow into a small discoverable SKILL.md with precise trigger conditions and testable commands. | ../../.agents/skills/create-skill/SKILL.md |
| Modify Skill | modify skill, update skill, スキル更新, スキル修正, skill maintenance | Safely update an existing SKILL.md when tools, triggers, commands or failure handling change, without widening its scope accidentally. | ../../.agents/skills/modify-skill/SKILL.md |
| OKF Wiki Operations | okf, OKF, LLM-Wiki, 知識ベース, wiki検索, 文脈解決, リンク切れ, 索引更新 | Operate an OKF v0.2 Markdown knowledge base safely with progressive disclosure, bounded context, weighted traversal, validation and relative links. | ../../.agents/skills/okf-wiki/SKILL.md |
| Patent to Wiki Ingestion | ingest patent, patent to wiki, 特許取り込み, 特許Wiki, EPO取り込み, family stubs, ingest-epo | Orchestrate cache-first EPO patent retrieval into an OKF wiki, then validate, index and smoke-test the resulting Markdown graph. | ../../.agents/skills/patent-to-wiki/SKILL.md |

<!-- LEARNED -->
<!-- Dynamic runtime additions appended by Maestro EvolutionLoop will be placed below this line -->
