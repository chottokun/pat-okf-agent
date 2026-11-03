---
name: Create Skill
description: Distill a repeated, verified workflow into a small discoverable SKILL.md with precise trigger conditions and testable commands.
trigger_conditions:
  - create skill
  - new skill
  - スキル作成
  - スキル化
  - distill workflow
---

# Create Skill

Use this meta-skill when a workflow has been repeated enough to deserve a
reusable instruction file. A skill contains judgement and sequencing; it should
delegate deterministic work to tools.

## Process

1. Identify the repeated task and its failure modes. Do not create a skill for a
   one-off request.
2. Confirm that no existing skill already covers it. Prefer `modify-skill`.
3. Choose a short kebab-case directory name.
4. Write `.agents/skills/<name>/SKILL.md` with YAML frontmatter:

```yaml
---
name: Human Readable Name
description: One sentence describing when and why to use this skill.
trigger_conditions:
  - specific phrase
  - domain term
---
```

5. Keep the body operational:
   - purpose and scope
   - non-negotiables
   - ordered workflow
   - exact commands or tool names
   - verification and failure handling
6. Do not embed credentials, tokens, personal data, or secrets.
7. Run:

```bash
uv run okf skills . --check --json
```

8. Bind the skill into a wiki when appropriate:

```bash
uv run okf init <wiki> \
  --skills-dir .agents/skills
```

## Quality bar

- Trigger phrases are specific enough to avoid loading the skill for every task.
- Commands are copy-pasteable and use `uv run` from the repository root.
- The skill does not duplicate large tool documentation; it points to the tool.
- Every destructive step has a validation or rollback instruction.
- The skill can be read in under two minutes.
