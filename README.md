# Rail Dashboard

Live deployment is configured via `render.yaml`.

## Local run

```powershell
.\.venv\Scripts\python.exe app.py
```

## Render deployment

- Connect this repo in Render.
- Render will use `render.yaml` automatically.
- Start command: `gunicorn app:app`
