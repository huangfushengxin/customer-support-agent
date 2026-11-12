# Python 版本与项目 requires-python >=3.14 对应。
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    AGENT_DATA_DIR=/data

# 依赖文件先复制，业务代码修改时可以复用依赖安装缓存。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "ticket_api:app", "--host", "0.0.0.0", "--port", "8000"]
