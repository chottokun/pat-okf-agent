# Project skills

This directory is the versioned, project-local skill source.

```text
.agents/skills/<name>/SKILL.md
```

Each skill must have YAML frontmatter with:

- `name`: human-readable skill name
- `description`: one sentence describing the capability
- `trigger_conditions`: a YAML list of task phrases that should activate it

Precedence:

1. `.agents/skills/` in the current wiki
2. `.agents/skills/` in the nearest ancestor project
3. `.opencode/skills/` in the nearest ancestor tooling directory

Use `okf skills <wiki> --check` to validate discovery and metadata.
Use `okf init <wiki> --skills-dir .agents/skills` to bind these skills into the
wiki's generated `.agents/AGENTS.md` table.
