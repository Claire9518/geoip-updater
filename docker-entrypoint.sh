#!/bin/bash
set -e

# 开启调试模式
set -x

# 确保 .env 使用 Unix 换行符
if [ -f /app/.env ]; then
    echo "Converting .env to Unix format..."
    sed -i 's/\r$//' /app/.env
    
    echo "Loading .env file..."
    # 使用更安全的方式加载环境变量
    while IFS= read -r line || [ -n "$line" ]; do
        # 跳过注释和空行
        if [[ $line =~ ^[[:space:]]*# ]] || [[ -z $line ]]; then
            continue
        fi
        # 去掉可能的注释
        line=$(echo "$line" | sed 's/[[:space:]]*#.*$//')
        # 导出环境变量
        export "$line"
    done < /app/.env
fi

# 打印所有环境变量
echo "Debug: All environment variables:"
env

AWS_CONFIG_DIR=~/.aws
export AWS_CONFIG_DIR

# 打印环境变量，验证是否正确传入
echo "Debug: Environment variables"
echo "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}"
echo "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}"
echo "AWS_PROFILE=${AWS_PROFILE}"
echo "AWS_REGION=${AWS_REGION}"

# 创建必要的目录和文件
mkdir -p ${AWS_CONFIG_DIR}
touch /var/log/cron.log

# 检查变量是否为空，如果为空则设置默认值或退出
if [ -z "${AWS_ACCESS_KEY_ID}" ]; then
    echo "Error: AWS_ACCESS_KEY_ID is not set"
    exit 1
fi

if [ -z "${AWS_SECRET_ACCESS_KEY}" ]; then
    echo "Error: AWS_SECRET_ACCESS_KEY is not set"
    exit 1
fi

# 设置 AWS 凭证（使用引号确保变量正确展开）
cat > "${AWS_CONFIG_DIR}/credentials" << EOF
[${AWS_PROFILE:-geoip-updater}]
aws_access_key_id=${AWS_ACCESS_KEY_ID}
aws_secret_access_key=${AWS_SECRET_ACCESS_KEY}
EOF

cat > "${AWS_CONFIG_DIR}/config" << EOF
[profile ${AWS_PROFILE:-geoip-updater}]
region=${AWS_REGION:-us-east-2}
output=json
EOF

# 验证文件内容
echo "Debug: Checking credentials file content"
cat "${AWS_CONFIG_DIR}/credentials"
echo "Debug: Checking config file content"
cat "${AWS_CONFIG_DIR}/config"

# 设置正确的权限
chmod 600 "${AWS_CONFIG_DIR}/credentials"
chmod 600 "${AWS_CONFIG_DIR}/config"
chmod 644 /var/log/cron.log

# 设置 cron 任务
echo "PATH=/usr/local/bin:/usr/bin:/bin" > /etc/cron.d/geoip-updater
echo "${CRON_SCHEDULE:-0 0 * * *} python /app/geoip_updater.py >> /var/log/cron.log 2>&1" > /etc/cron.d/geoip-updater
chmod 0644 /etc/cron.d/geoip-updater
crontab /etc/cron.d/geoip-updater

# 启动 cron
service cron start

# 使用 tail -F
exec tail -F /var/log/cron.log