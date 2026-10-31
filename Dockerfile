FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

ARG PI_PACKAGE=@earendil-works/pi-coding-agent@1.4.0
ARG NODE_VERSION=22.23.4-1nodesource1
ARG NODESOURCE_KEY_SHA256=b42e0321dabdc24e892115da705cf061167eac12a317f23d329862d0aa0a271d
ARG SANDBOX_USER=sunridge
ARG SANDBOX_UID=10001

ENV LANG=C.UTF-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/repo/tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg tini \
    && curl -fsSL -o /tmp/nodesource.key https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    && echo "$NODESOURCE_KEY_SHA256  /tmp/nodesource.key" | sha256sum -c - \
    && gpg --dearmor -o /usr/share/keyrings/nodesource.gpg /tmp/nodesource.key \
    && rm /tmp/nodesource.key \
    && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "nodejs=$NODE_VERSION" \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --ignore-scripts "$PI_PACKAGE" \
    && npm cache clean --force \
    && useradd --create-home --uid "$SANDBOX_UID" --shell /bin/bash "$SANDBOX_USER" \
    && chmod 0711 "$(readlink -f "$(command -v node)")" \
    && install -d -m 0700 /var/lib/trades-bench /var/lib/trades-bench/results

WORKDIR /repo

COPY data/geography-arizona-v1 data/geography-arizona-v1
RUN test -s data/geography-arizona-v1/addresses/catalog.csv \
    && test -s data/geography-arizona-v1/routing/anchor_distance.bin \
    || { echo "run 'sh tools/fetch_geography.sh' before building" >&2; exit 1; }

COPY pyproject.toml uv.lock README.md .python-version ./
COPY src src
COPY tools tools
RUN uv sync --frozen --no-dev \
    && chmod 0700 /repo /root

ENTRYPOINT ["tini", "-g", "--", "/repo/.venv/bin/python", "-m", "policies.container_run"]
