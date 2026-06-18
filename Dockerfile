FROM ubuntu:22.04

ENV PYTHONUNBUFFERED=1 \
    PORT=8765 \
    CLAUDE_HTTP_MOCK=false \
    PYPI_MIRROR=http://mirrors.aliyun.com/pypi/simple \
    PYPI_MIRROR_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

WORKDIR /home/admin/claude-code-scripts

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    libbz2-dev \
    libffi-dev \
    liblzma-dev \
    libreadline-dev \
    libsqlite3-dev \
    libssl-dev \
    tk-dev \
    make \
    wget \
    xz-utils \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

ARG PYTHON_VERSION=3.11.13
RUN mkdir -p /opt/temp \
    && cd /opt/temp \
    && wget -q https://registry.npmmirror.com/-/binary/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz \
    && tar xzf Python-${PYTHON_VERSION}.tgz \
    && cd Python-${PYTHON_VERSION} \
    && ./configure --enable-optimizations \
    && make -j"$(nproc)" \
    && make install \
    && cd / \
    && rm -rf /opt/temp

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -i ${PYPI_MIRROR} --trusted-host ${PYPI_MIRROR_HOST} -r requirements.txt

ARG CLAUDE_CODE_TGZ=""
COPY . .

RUN if [ -n "$CLAUDE_CODE_TGZ" ] && [ -f "$CLAUDE_CODE_TGZ" ]; then \
      npm install -g "$CLAUDE_CODE_TGZ"; \
    else \
      npm pack @anthropic-ai/claude-code --registry=https://registry.npmmirror.com \
      && npm install -g ./anthropic-ai-claude-code-*.tgz \
      && rm -f ./anthropic-ai-claude-code-*.tgz; \
    fi

RUN chmod +x docker/sandbox_start.sh scripts/*.sh

EXPOSE 8765
CMD ["./docker/sandbox_start.sh"]
