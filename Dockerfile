FROM python:3.10-slim

WORKDIR /app

# 安装必要的系统包
RUN apt-get update && \
    apt-get install -y \
    cron \
    curl \
    unzip \
    python3-pip \
    groff \
    less \
    && rm -rf /var/lib/apt/lists/*

# 安装 AWS CLI
RUN curl "https://s3.amazonaws.com/aws-cli/awscli-bundle.zip" -o "awscli-bundle.zip" && \
    unzip awscli-bundle.zip && \
    ./awscli-bundle/install -b /usr/local/bin/aws && \
    rm -rf awscli-bundle.zip awscli-bundle

# 创建日志目录并设置权限
RUN mkdir -p /var/log && \
    touch /var/log/cron.log && \
    chmod 644 /var/log/cron.log

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码和启动脚本
COPY geoip_updater.py .
COPY docker-entrypoint.sh .
COPY .env .

# 设置环境变量
ENV AWS_CONFIG_DIR=/root/.aws
ENV TZ=Asia/Shanghai

# 确保脚本使用 Unix 行尾结束符并具有执行权限
RUN sed -i 's/\r$//' ./docker-entrypoint.sh && \
    chmod +x ./docker-entrypoint.sh

# 设置入口点
ENTRYPOINT ["./docker-entrypoint.sh"]