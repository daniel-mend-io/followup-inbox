---
name: dst-changeover
description: Every routine cron is fixed UTC, so local firing times shift by an hour at each DST changeover unless bumped
metadata:
  type: project
---

Routine schedules are 5-field crons in UTC. When the configured `timezone`
changes its offset (DST start and end), every routine fires an hour earlier or
later in local terms.

**Why:** the scheduler has no notion of the user's zone; the previous setup
lost an hour on 2026-10-25 this way and the "morning" pass ran mid-morning.

**How to apply:** twice a year run `python3 scripts/render.py --schedule`,
compare today's local times with the intended ones, and edit the crons in
`config.yml` and in the routines UI. Or accept the drift; the routines still
run, only the local time moves.
