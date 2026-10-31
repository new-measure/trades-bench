# Sandbox the agent's recorded shell commands are re-executed in during a rebuild.
# Mirrors the original agent host: Ubuntu, user `ubuntu`, home /home/ubuntu.
FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 curl jq sqlite3 socat ca-certificates bc \
    && rm -rf /var/lib/apt/lists/*
USER ubuntu
WORKDIR /home/ubuntu
CMD ["sleep", "infinity"]
