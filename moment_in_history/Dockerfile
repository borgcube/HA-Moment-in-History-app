FROM python:3.13-slim-bookworm

LABEL \
  io.hass.version="1.9.15" \
  io.hass.type="app" \
  io.hass.arch="amd64"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY moment_in_history.py /app/moment_in_history.py
COPY config_ui.py /app/config_ui.py

CMD ["python", "/app/moment_in_history.py", "--daemon"]
