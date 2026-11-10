#!/bin/sh
# Sites change often and break old yt-dlp versions: pull the newest one on every start
# (into /data/.local, which wins over the bundled copy). Never block startup on it.
timeout 120 pip install --user --upgrade --quiet --disable-pip-version-check "yt-dlp[default]" \
  || echo "yt-dlp self-update failed; using the bundled version"
python -c 'import yt_dlp.version as v; print("yt-dlp", v.__version__)'
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
