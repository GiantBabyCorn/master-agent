FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

ENV PATH="/root/.local/bin:${PATH}"

ARG INSTALL_NODEJS=false
ARG CURSOR_CLI_INSTALL_CMD=""
ARG ANTHROPIC_CLI_INSTALL_CMD=""
ARG CODEX_CLI_INSTALL_CMD=""

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates bash && rm -rf /var/lib/apt/lists/*

RUN if [ "${INSTALL_NODEJS}" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/* ; \
    fi

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir -e .

RUN if [ -n "${CURSOR_CLI_INSTALL_CMD}" ]; then bash -lc "${CURSOR_CLI_INSTALL_CMD}"; fi
RUN if [ -n "${ANTHROPIC_CLI_INSTALL_CMD}" ]; then bash -lc "${ANTHROPIC_CLI_INSTALL_CMD}"; fi
RUN if [ -n "${CODEX_CLI_INSTALL_CMD}" ]; then bash -lc "${CODEX_CLI_INSTALL_CMD}"; fi

EXPOSE 3000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
