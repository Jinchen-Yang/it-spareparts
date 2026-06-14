FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 包索引可由构建参数覆盖（国内服务器走镜像，默认上游 PyPI；勿在源码硬编码地域镜像）
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120

# 先装依赖，利用层缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir --retries 5 --upgrade pip \
    && pip install --no-cache-dir --retries 5 .

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
