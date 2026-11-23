# lexchat-eval, the evaluation harness, as an image to run beside AILA.
# Built on the server from a release tag, from the repo root:
#   sudo docker compose --env-file /etc/lexeval/settings.env -f deploy/compose.yml build eval
# No secret is copied in: settings come from the env file at run time.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The root filesystem is read-only: everything written goes to /tmp or a volume.
    HOME=/tmp \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    LEX_EVAL_DB=/var/lib/lexeval/responses.db \
    LEX_EVAL_BACKUP_DIR=/var/backups/lexeval \
    # Streamlit would otherwise send usage statistics to an outside host.
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true

# Exact versions with hashes, so a rebuild installs exactly what was tested.
COPY deploy/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

# A user with no login, owning only the two directories the volumes mount on.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin lexeval \
 && mkdir -p /var/lib/lexeval /var/backups/lexeval \
 && chown lexeval:lexeval /var/lib/lexeval /var/backups/lexeval

WORKDIR /app
COPY pyproject.toml ./
COPY lex_eval ./lex_eval
COPY --chmod=0755 deploy/lexeval.sh /usr/local/bin/lexeval

USER lexeval
ENTRYPOINT ["lexeval"]
CMD ["help"]
