# Cloudhop in one container: the API, the background job, the dashboard and the homepage, served from one address
# (sign-in cookies stay first-party). Settings come from the environment; nothing secret is built into the image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/cloudhop.db

WORKDIR /app/backend
# Dependencies only, from pyproject.toml, in their own layer. The app itself is not pip-installed: it finds its
# templates and the frontend next to its own files.
COPY backend/pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY backend/cloudhop ./cloudhop
COPY backend/templates ./templates
COPY frontend/public /app/frontend/public

# Runs as an unprivileged user; the SQLite database lives on the /data volume.
RUN useradd --system --uid 10001 --home-dir /app cloudhop && mkdir -p /data && chown cloudhop /data
USER cloudhop
VOLUME ["/data"]

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/', timeout=4)"
CMD ["python", "-m", "uvicorn", "cloudhop.main:app", "--host", "0.0.0.0", "--port", "8787"]
