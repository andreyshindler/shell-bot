FROM python:3.12-slim

RUN groupadd --gid 1000 botuser \
    && useradd --uid 1000 --gid botuser --create-home --shell /bin/bash botuser

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
