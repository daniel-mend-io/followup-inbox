## Refresh the release dates

Snoozing an item to `next-release` wakes it on the Friday before the next release deploys. The dates come from `state/releases.json`, which only this step keeps current, because snoozing happens where there is no calendar.

List the events on the calendar named "{{releases.calendar}}" (find its id with `list_calendars`) from 30 days ago to 6 months ahead. Keep the events whose title matches the regular expression `{{releases.title_regex}}`; the first group is the version. Titles can carry extra words after the match ("26.9.1 Deployments + Self-hosted (delayed)"); the match still counts. For each, `date` is the day the event starts **in that calendar's own timezone** (an event that starts at 23:00 the evening before in another zone belongs to the next day). Then replace the list:

```
{{scripts}}/releases.py set --json <<'JSON'
[{"version": "26.9.3", "date": "2026-10-18", "title": "26.9.3 Deployments"}]
JSON
```

Stage `state/releases.json` with the rest of this run's paths when you save. The release dates are saved even on a run that is otherwise quiet: if nothing else changes but `git status --porcelain state/releases.json` shows the file changed, run `{{scripts}}/sync.py -m "releases: refreshed from the calendar" state/releases.json` and stop there, without a message. If the calendar cannot be read, leave the file as it is and say so in the report (if there is one).
