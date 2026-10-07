## What the user's past replies taught

Before you judge or draft anything, read what the store has learned:

```
{{scripts}}/knowledge.py context
```

It is distilled from items the user already closed: how what they actually sent differed from what was drafted, durable facts about the product, the people and the process, and what they dismiss. Use it like this:
- **Learned voice and the examples outrank the configured voice line.** Where they disagree, follow the learned ones: they come from what the user sent. Match the examples' length, opening and level of detail, not just their tone.
- **Facts carry a count and a date.** A fact seen once, or last confirmed weeks ago, is a lead, not ground truth. When a draft leans on one (a version, an owner, a date, a meeting slot), check it at the source first. Never quote knowledge to anyone as if it came from the thread.
- **Triage rules say what not to raise.** Before recording an item, check it against the Triage section. If a rule plainly covers it, leave it out and mention it in your report as skipped by a learned rule, one line, so a bad rule gets noticed.
- If it prints `(nothing learned yet)`, carry on with the configured voice.

Never write to `knowledge/`; only the learn routine does.
