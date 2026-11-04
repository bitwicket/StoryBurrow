
FROM ghcr.io/selkies-project/selkies/base:2.1.0-ubuntu26.04

USER 0

#Build StoryBurrow directories.
RUN mkdir -p /app /games /saves /data /logs && \
    chown -R 1000:1000 \
      /app /games /saves /data /logs

#Register StoryBurrow as an independent s6-managed service.
COPY --chown=1000:1000 --chmod=755 \
    services/storyburrow/run \
    /etc/service/storyburrow/run

# StoryBurrow web application dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-venv \
      megatools && \
    python3 -m venv /opt/storyburrow-venv && \
    /opt/storyburrow-venv/bin/pip install \
      --no-cache-dir \
      requests \
      Pillow \
      'Flask==3.1.2' \
      'ecdsa==0.19.1' && \
    rm -rf /var/lib/apt/lists/*

#Set ownhership
COPY --chown=1000:1000 app/ /app/

#Register the web server with s6
COPY --chown=1000:1000 --chmod=755 services/web/run \
    /etc/service/web/run

#Add Caddy web server for reverse proxy and TLS termination.
RUN apt-get update && apt-get install -y --no-install-recommends caddy && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /certs /data/caddy-config /etc/service/caddy && chown -R 1000:1000 /certs /data/caddy-config /etc/service/caddy
COPY --chown=1000:1000 Caddyfile /app/Caddyfile
COPY --chown=1000:1000 --chmod=755 services/caddy/run /etc/service/caddy/run

# Restore Selkies' expected runtime user.
USER 1000

WORKDIR /app

EXPOSE 6080 2500

# Preserve Selkies' native initialization.
# ENTRYPOINT ["/etc/container-entrypoint.sh"]
