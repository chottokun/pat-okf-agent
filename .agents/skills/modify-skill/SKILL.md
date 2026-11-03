---
name: Modify Skill
description: Safely update an existing SKILL.md when tools, triggers, commands or failure handling change, without widening its scope accidentally.
trigger_conditions:
  - modify skill
  - update skill
  - スキル更新
  - スキル修正
  - skill maintenance
---

# Modify Skill

Use this meta-skill when an existing skill is stale, too broad, or missing a
verified failure path.

## Process

1. Read the entire target `SKILL.md` and its frontmatter.
2. Identify the smallest change:
   - wrong command or path
   - missing prerequisite
   - missing failure handling
   - trigger too broad or too narrow
3. Preserve the skill's scope unless the task explicitly changes it.
4. Update `trigger_conditions` only with terms that reliably indicate this task.
5. Do not add secrets or environment-specific absolute paths unless unavoidable.
6. Run:

```bash
uv run okf skills . --check --json
```

7. If the skill is bound in a wiki, regenerate `AGENTS.md`:

```bash
uv run okf init <wiki> \
  --skills-dir .agents/skills
```

8. Verify that the generated table contains the new trigger text and that the
   `<!-- LEARNED -->` tail was preserved.

## Change checklist

- [ ] Frontmatter still has `name`, `description`, `trigger_conditions`
- [ ] Commands still work with the current CLI
- [ ] Failure paths are concrete
- [ ] No secrets added
- [ ] Scope did not silently expand
- [ ] `AGENTS.md` regenerated if the skill is bound
