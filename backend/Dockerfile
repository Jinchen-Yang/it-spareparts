FROM --platform=linux/amd64 python:3.11-slim@sha256:00af38ae2ed311628970782e8a2d7f014d8909dbc63cb97bc0a158187f4db045

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 包索引可由构建参数覆盖（国内服务器走镜像，默认上游 PyPI；勿在源码硬编码地域镜像）
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120

# 依赖闭包由 committed uv.lock 导出的 requirements.lock 固定并校验 hash。
COPY requirements.lock uv.lock pyproject.toml ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
