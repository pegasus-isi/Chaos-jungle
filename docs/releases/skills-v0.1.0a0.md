# Chaos Jungle skills 0.1.0a0

This alpha release contains one optional assistant-skills ZIP for Chaos Jungle
`0.1.0a0`. It is separate from the Python package.

## Included skills

- `chaos-jungle-injection`: choose a fault, target, injection boundary, safety
  limit, and evidence of manifestation and recovery.
- `chaos-jungle-evaluation`: design matched controls, workloads, scoring,
  exposure checks, uncertainty analysis, and reproducible reports.

The ZIP preserves each skill's `SKILL.md` and supporting reference files.

## Install

Download `chaos-jungle-skills-v0.1.0a0.zip` from this release, then extract
both skill folders into your assistant's skills directory. For Codex:

```sh
unzip chaos-jungle-skills-v0.1.0a0.zip -d ~/.agents/skills/
```

Start a new Codex session after installation. Install the Python wheel
separately from the [Chaos Jungle 0.1.0a0 package release](https://github.com/pegasus-isi/Chaos-jungle/releases/tag/v0.1.0a0).
The skills are instructions; they do not install CJ, grant target permissions,
or verify that an injected fault reached a workload.
