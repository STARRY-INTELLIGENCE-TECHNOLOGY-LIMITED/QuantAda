# Optimization Task Resume Guide

> Moved from the README without content changes. Runtime log spec: [`docs/specs/workflows/optimizer_runtime.md`](../specs/workflows/optimizer_runtime.md).

Choose a previous training task interactively and resume its saved configuration:

```bash
python run.py --train_resume
```

Optimization tasks use a stable identity hash derived from the complete training configuration. Repeating the same training resumes safely or reports that it is already running; changing parameters creates an independent Journal instead of being rejected by an unrelated running task.

Tasks are listed by most recent update, ten per page. Use `n` / `p` to browse or `g 3` to jump to page 3. Select a task to inspect its original command, then enter `y` to confirm, `b` to return, or `q` to exit. The web workspace provides the same pagination and command details; click **Resume selected task** to confirm.
The list also shows training status from the display section at the end of the matching terminal log. The journal filename must match; a window or snapshot written on the log must match the task too. A log with neither discriminator is not applied to every task that shares one journal file. Markers do not finish a task when the suffix is an empty crash summary, such as no metric result or both metrics and trials completed being zero; those logs are skipped. A missing end marker means incomplete. Incomplete tasks can still be resumed.
After selecting a task, enter `l` to page through that task's matching terminal log. It does not open another task's log, and it opens at the end. Use `n` / `p` to move, `h` / `e` / `m` to jump to the first, last, or middle page, `s` for the analysis section, `g 3` for page 3, and `b` to return. The web workspace **View log** button provides the same paging.
Resume prefers the saved data, selection, option universe, and train/test snapshot across dates. Only tasks with complete training metadata can be resumed. The details provide a copyable fresh-data command that removes study bindings and adds `--refresh` to rerun selection, fetch data, and train independently. Omitted dates are inferred at launch; explicit dates can be edited manually.
Tasks without finite-grid metadata or other required metadata are omitted from the resume list and are not counted toward a new budget. Start a new Study name or Journal for a fresh run. Current Study trial numbers match the Journal trial_id.

