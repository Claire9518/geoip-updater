# GeoIP Updater

自动更新 AWS Lambda Layer 中的 MaxMind GeoIP 数据库的 Docker 镜像。

## 特性

- 🔄 自动更新 GeoIP 数据库
- ⚡ 支持 AWS Lambda Layer 更新
- 🕒 内置定时任务功能
- ⚙️ 灵活的配置选项
- 🐳 Docker 容器化部署

## 快速开始

### 使用 Docker Compose（推荐）

1. 创建 docker-compose.yml：

```
yaml
version: '3'

services:
  geoip-updater:
    image: yourusername/geoip-updater:latest
    container_name: geoip-updater
    restart: unless-stopped
    env_file:
      - .env
```

2. 创建 .env 文件：

```
# AWS Configuration
AWS_ACCESS_KEY_ID=your_access_key_id
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_PROFILE=geoip-updater
AWS_REGION=us-east-2

# Lambda Configuration
LAMBDA_LAYER_NAME=GeoLite2

# GeoIP Database Configuration
GEOIP_DOWNLOAD_URL=https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb

# Cron Configuration
CRON_SCHEDULE=0 0 * * *

# Timezone
TZ=Asia/Shanghai
```

3. 启动服务：

```
docker-compose up -d
```

### 使用 Docker 命令

```
docker run -d \
  --name geoip-updater \
  --restart unless-stopped \
  --env-file .env \
  Claire9518/geoip-updater:latest
```

### 配置说明

#### 环境变量

| 变量名                | 描述                 | 必需 | 默认值        |
| --------------------- | -------------------- | ---- | ------------- |
| AWS_ACCESS_KEY_ID     | AWS 访问密钥 ID      | 是   | -             |
| AWS_SECRET_ACCESS_KEY | AWS 访问密钥         | 是   | -             |
| AWS_PROFILE           | AWS 配置文件名称     | 否   | geoip-updater |
| AWS_REGION            | AWS 区域             | 否   | us-east-2     |
| LAMBDA_LAYER_NAME     | Lambda Layer 名称    | 否   | GeoLite2      |
| GEOIP_DOWNLOAD_URL    | GeoIP 数据库下载地址 | 否   | (默认地址)    |
| CRON_SCHEDULE         | Cron 更新计划        | 否   | 0 0 * * *     |
| TZ                    | 时区                 | 否   | Asia/Shanghai |

#### 命令行参数

支持以下命令行参数：

* `--action`: 选择操作模式
  * `update`: 执行更新（默认）
  * `check`: 检查状态
  * `schedule`: 启动定时任务
  * `test-update`: 测试更新层
  * `test-function`: 测试函数更新
  * `cleanup`: 清理临时文件

例如：
```
docker run Claire9518/geoip-updater:latest --action check
```

### 使用示例

#### 1. 检查当前状态
```docker run --env-file .env Claire9518/geoip-updater:latest --action check```

#### 2. 执行一次性更新

```docker run --env-file .env Claire9518/geoip-updater:latest --action update```
#### 3. 启动定时更新服务
```docker run -d --env-file .env Claire9518/geoip-updater:latest --action schedule```

### 日志查看

```
# 查看容器日志
docker logs geoip-updater

# 实时跟踪日志
docker logs -f geoip-updater
```
