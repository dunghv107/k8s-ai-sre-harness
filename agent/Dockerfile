FROM python:3.13.18-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN /opt/venv/bin/pip install -r requirements.txt
COPY pyproject.toml ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps . && /opt/venv/bin/pip uninstall -y pip

FROM python:3.13.18-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_DB_PATH=/data/agent.db
COPY --from=build /opt/venv /opt/venv
# pip vendors msgpack/urllib3 copies with open CVEs and is not needed at runtime
RUN pip uninstall -y pip
# /data is the PVC mount in the cluster; the chown only matters for plain docker run.
RUN mkdir /data && chown 10001:10001 /data
USER 10001
WORKDIR /data
EXPOSE 8080
CMD ["uvicorn", "alert_agent.app:app", "--host", "0.0.0.0", "--port", "8080"]
