# drivecast 单进程镜像:Web + 内嵌 worker(docs/06 §12)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 drivecast
WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY --chown=drivecast:drivecast . .
# data/ 由卷挂载;预建并交给运行用户,空卷首次挂载时继承该属主
RUN mkdir -p /app/data && chown drivecast:drivecast /app/data

USER drivecast
VOLUME ["/app/data"]
EXPOSE 8080

# 非 loopback 监听时 run.py 要求 AUTH_SECRET,须经 -e/--env-file 注入,切勿写进镜像
CMD ["python", "run.py"]
