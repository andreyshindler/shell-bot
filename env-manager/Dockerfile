FROM python:3.12-slim

# Match the UID/GID of the host user that owns the bind-mounted projects dir
# (see docker-compose.yml), so this can actually write edited .env files.
ARG UID=1000
ARG GID=1000

RUN groupadd --gid ${GID} envmgr \
    && useradd --uid ${UID} --gid envmgr --create-home --shell /usr/sbin/nologin envmgr

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

RUN chown -R envmgr:envmgr /app
USER envmgr

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
