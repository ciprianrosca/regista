One JSON file per build step, written by `/daedalus:build` when the step starts:

    {"step": "stats", "milestone": "M1", "title": "Mean and median", "state": "active",
     "interface": "calc.stats.mean(xs) -> float",
     "requirements": [{"id": "STATS-1", "text": "WHEN xs is empty, mean SHALL raise ValueError", "verify": "test"}]}

Tests name the IDs they cover (`# covers: STATS-1`). Whether a requirement passes is decided only by
the gates (`.daedalus/gates/requirements.json`); `dd.py requirements` and `dd.py milestone M1` show it.
`"state": "planned"` keeps a spec written ahead of time from blocking commits.
