# Reelshift-graph base image — used by all agency deployments
FROM python:3.12-slim-bookworm

# Install ffmpeg + tini (proper PID 1 for signal handling)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        ca-certificates \
        tzdata && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user inside the container.
# UID/GID are set at runtime via docker-compose to match the host's
# per-agency user, so volume mounts work cleanly.
ARG APP_UID=999
ARG APP_GID=987
RUN groupadd -g ${APP_GID} reelshift && \
    useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash reelshift

# Working directory + Python deps
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the source code (read-only, owned by root)
COPY *.py /app/

# /data is where each deployment mounts its configs/, state/, .env
RUN mkdir -p /data && chown reelshift:reelshift /data

# Switch to non-root user
USER reelshift

# Use tini as init for clean signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "main.py"]
