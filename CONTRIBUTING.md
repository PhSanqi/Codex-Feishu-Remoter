# Contributing

Keep changes narrow, preserve native Codex authority, and do not introduce a
parallel settings/session/job authority.

Before opening a pull request:

```powershell
python -m unittest discover -s tests/unit -p "test*.py"
python scripts/check_public_release.py

cd m3_control
npm ci
npm run build
```

Do not include credentials, local databases, runtime evidence, generated
frontend assets, source snapshots, Codex session state, or user workspace data.
