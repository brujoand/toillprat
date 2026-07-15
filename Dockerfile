FROM python:3.12-slim

# A numeric UID, so a runtime enforcing "must not run as root" can check it
# without a passwd lookup.
RUN useradd --uid 10001 --no-create-home --system toillprat

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

WORKDIR /app
COPY toillprat /app/toillprat

# The version is stamped in at build time: semantic-release decides it from the
# commits, AFTER this repo has been written, so it is not knowable from inside
# the source tree. pyproject.toml says 0.0.0 for the same reason.
#
# The default is "dev", which is what a checkout should say -- and a deployed
# image that somehow says "dev" is telling you its build was wrong, which is more
# use than a confident lie.
ARG VERSION=dev
ENV APP_VERSION=${VERSION}

# Where characters, chats, and settings live. Mount a volume here to persist.
ENV DATA_DIR=/data
RUN mkdir -p /data && chown 10001 /data

# Bake the bytecode now, so the image still works with a read-only root
# filesystem, where Python could not write __pycache__ on import.
RUN python -m compileall -q /app/toillprat

USER 10001
EXPOSE 8080

CMD ["uvicorn", "toillprat.main:app", "--host", "0.0.0.0", "--port", "8080"]
