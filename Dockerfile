FROM python:3.12-slim
WORKDIR /app/ansible-mcp

# Install OS packages needed for Ansible usage
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client sshpass rsync git curl ca-certificates tini docker.io && rm -rf /var/lib/apt/lists/*
RUN apt-get update -y && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*

# Install Ansible and helpful linters
RUN pip install --no-cache-dir ansible ansible-lint yamllint docker requests PyYAML


# ENV PATH="/root/.local/bin:${PATH}"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "mcp_http:app", "--host", "0.0.0.0", "--port", "9000", "--proxy-headers", "--access-log", "--log-level", "info"]

# ビルドコンテキストは repo ルート想定
# COPY mcp-ansible-wrapper/ /app/
# （もしPythonパッケージなら）COPY pyproject.toml/requirements も併せて