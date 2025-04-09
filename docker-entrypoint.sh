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

# 处理 cron 任务 - 确保没有重复任务
CRON_FILE="/etc/cron.d/geoip-updater"

# 清空或创建新的cron文件
echo "# GeoIP Updater cron job" > "$CRON_FILE"
echo "PATH=/usr/local/bin:/usr/bin:/bin" >> "$CRON_FILE"
echo "${CRON_SCHEDULE:-0 0 * * *} /usr/local/bin/python /app/geoip_updater.py >> /var/log/cron.log 2>&1" >> "$CRON_FILE"
echo "# End of cron file" >> "$CRON_FILE"

echo "Debug: Cron file content:"
cat "$CRON_FILE"

# 设置cron文件权限
chmod 0644 "$CRON_FILE"

# 应用cron配置
crontab "$CRON_FILE"

# 检查crontab内容
echo "Debug: Current crontab content:"
crontab -l

# 确保没有旧的重复任务
# 重启cron服务以确保清除任何旧任务
service cron restart

echo "Cron service restarted and configuration applied."

# 启动 cron - 确保cron服务正在运行
service cron status || service cron start

# 使用 tail -F
exec tail -F /var/log/cron.log