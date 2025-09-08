FROM python:3.12-slim

# Install OS packages needed for Ansible usage
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       openssh-client sshpass rsync git curl ca-certificates tini \
       docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install Ansible and helpful linters
RUN pip install --no-cache-dir \
      ansible \
      ansible-lint \
      yamllint \
      docker \
      requests \
      PyYAML

WORKDIR /work

ENV PATH="/root/.local/bin:${PATH}"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
