# One container: Hermes gateway + hermes-ghost-call plugin + the ghost bridge (same lesson as
# hermes-phone: the plugin and the bridge must share one SQLite file, so they share a container).
# Base image internals: s6-overlay is PID 1, HERMES_HOME=/opt/data (VOLUME), venv at
# /opt/hermes/.venv, runtime user "hermes".
FROM nousresearch/hermes-agent:v0.22.7

USER root

COPY plugin.yaml __init__.py pyproject.toml README.md /opt/hermes-ghost-call/
COPY hermes_ghost_call/ /opt/hermes-ghost-call/hermes_ghost_call/
COPY skills/ /opt/hermes-ghost-call/skills/

# Plugin python deps into Hermes' venv, constrained to what the venv already has so Hermes' own
# pins never move (a conflict fails the build here instead of at runtime).
RUN set -eu; \
    UV="$(cd /opt/hermes && python3 -c 'from pm._uv import _toolchain; print(_toolchain(realize=False)[0])')"; \
    PY=/opt/hermes/.venv/bin/python; \
    "$UV" pip freeze --python "$PY" > /tmp/constraints.txt; \
    "$PY" -c 'import tomllib; print("\n".join(tomllib.load(open("/opt/hermes-ghost-call/pyproject.toml","rb"))["project"]["dependencies"]))' > /tmp/requirements.txt; \
    "$UV" pip install --no-cache --python "$PY" -r /tmp/requirements.txt -c /tmp/constraints.txt; \
    PYTHONPATH=/opt/hermes-ghost-call "$PY" -c 'import fastapi, uvicorn, websockets, httpx, hermes_ghost_call.bridge.app'; \
    rm -f /tmp/constraints.txt /tmp/requirements.txt

COPY --chmod=0755 deploy/03-hermes-ghost-call /etc/cont-init.d/03-hermes-ghost-call
COPY deploy/s6/hermes-ghost-call-bridge/ /etc/s6-overlay/s6-rc.d/hermes-ghost-call-bridge/
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/hermes-ghost-call-bridge/run \
    && touch /etc/s6-overlay/s6-rc.d/user/contents.d/hermes-ghost-call-bridge

# Plugin and bridge share state only through this file.
ENV HERMES_GHOST_DB=/opt/data/plugin-data/hermes-ghost-call/ghost.db
# Behind a proxy that overwrites X-Forwarded-For (Railway, Fly): rate-limit on it.
ENV HERMES_GHOST_TRUST_PROXY=1

CMD ["gateway", "run"]
