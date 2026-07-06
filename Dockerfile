FROM python:3.12-slim

# Match the UID/GID of the host user that owns the bind-mounted project dir
# (see docker-compose.yml), so files created by the bot (e.g. shell_bot.log)
# are writable by both the container and that host user.
ARG UID=1000
ARG GID=1000

RUN groupadd --gid ${GID} botuser \
    && useradd --uid ${UID} --gid botuser --create-home --shell /bin/bash botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shell_bot.py .

RUN chown -R botuser:botuser /app
USER botuser

# Default working directory for shell commands the bot runs (WORKING_DIR
# defaults to the user's home). Mount a volume here to persist state
# (e.g. cloned repos) across container restarts.
WORKDIR /home/botuser

ENTRYPOINT ["python3", "/app/shell_bot.py"]
