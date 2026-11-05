FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ARG SOURCE_COMMIT
LABEL org.opencontainers.image.source="https://github.com/brad888/shadow-saver" org.opencontainers.image.revision=$SOURCE_COMMIT
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SOURCE_COMMIT=$SOURCE_COMMIT
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home saver && mkdir /data && chown saver /data
COPY shadow_saver ./shadow_saver
USER saver
ENV DATABASE_PATH=/data/queries.db PILOT_DATABASE_PATH=/data/pilot.db
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).close()"
EXPOSE 8000
CMD ["uvicorn", "shadow_saver.mobile:create_mobile_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
