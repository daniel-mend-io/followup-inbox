# state/

The module's own operational notes: routine ids, caveats, things learned in
production that are not derivable from the code. One fact per file, memory-file
shaped, so an agent can recall them selectively:

```markdown
---
name: short-kebab-slug
description: one line, used to decide relevance
metadata:
  type: project | reference | feedback
---

The fact. For a caveat, follow with **Why:** and **How to apply:**.
```

Add a one-line pointer here per file.

- [DST changeover shifts every cron](dst-changeover.md) — crons are UTC; local times drift twice a year

Instance-specific state (which routines were created, their ids) belongs in
the instance repository's own `state/`, in the same shape, not here.
