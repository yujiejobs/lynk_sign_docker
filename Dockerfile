FROM python:3.12-slim

# 时区: cron 按此触发, 可在 docker-compose 用 TZ 覆盖.
# 镜像不含系统时区数据库, 代码里用 zoneinfo, 拿不到则退化成固定 +8 偏移 (见 LYNK_TZ_OFFSET).
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    LYNK_CONFIG_FILE=/data/config.json \
    LYNK_LOGS_DIR=/data/logs

WORKDIR /app

# 唯一第三方依赖是 requests; cron 用自带的轻量实现, 无需 croniter.
# 若默认 pip 源慢, 可换镜像: docker compose build --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX=
COPY requirements.txt .
RUN if [ -n "$PIP_INDEX" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX" -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

COPY ql_lynk.py config_server.py config.html ./

VOLUME ["/data"]
EXPOSE 8787

CMD ["python", "config_server.py", "--host", "0.0.0.0", "--port", "8787", "--no-browser"]
