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
    image: Claire9518/geoip-updater:latest
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

# ASN Configuration (可选)
# 启用后，ASN mmdb 会与 City mmdb 一起打包进同一个 Layer
ASN_ENABLED=false
ASN_MAXMIND_EDITION_ID=GeoLite2-ASN
ASN_MAXMIND_SUFFIX=tar.gz
ASN_DOWNLOAD_URL=https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-ASN.mmdb

# Hash Compare Mode (city / asn / both / none)
HASH_COMPARE_MODE=both

# Log Rotation
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5

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

| 变量名                 | 描述                                     | 必需 | 默认值          |
| ---------------------- | ---------------------------------------- | ---- | --------------- |
| AWS_ACCESS_KEY_ID      | AWS 访问密钥 ID                          | 是   | -               |
| AWS_SECRET_ACCESS_KEY  | AWS 访问密钥                             | 是   | -               |
| AWS_PROFILE            | AWS 配置文件名称                         | 否   | geoip-updater   |
| AWS_REGION             | AWS 区域                                 | 否   | us-east-2       |
| LAMBDA_LAYER_NAME      | Lambda Layer 名称                        | 否   | GeoLite2        |
| GEOIP_DOWNLOAD_URL     | GeoIP 数据库下载地址                     | 否   | (默认地址)      |
| CRON_SCHEDULE          | Cron 更新计划                            | 否   | 0 0 * * *       |
| TZ                     | 时区                                     | 否   | Asia/Shanghai   |
| ASN_ENABLED            | 启用 ASN mmdb 下载与推送                 | 否   | false           |
| ASN_MAXMIND_EDITION_ID | MaxMind ASN 版本 ID                      | 否   | GeoLite2-ASN    |
| ASN_MAXMIND_SUFFIX     | MaxMind 下载文件后缀                     | 否   | tar.gz          |
| ASN_DOWNLOAD_URL       | ASN mmdb 备用下载地址                    | 否   | (空)            |
| HASH_COMPARE_MODE      | 触发更新的 hash 类型: city/asn/both/none | 否   | both            |
| LOG_MAX_BYTES          | 日志文件最大字节数                       | 否   | 10485760 (10MB) |
| LOG_BACKUP_COUNT       | 日志备份数量                             | 否   | 5               |

#### ASN 说明

- `ASN_ENABLED=true` 时，`GeoLite2-ASN.mmdb` 与 `GeoLite2-City.mmdb` 会打包进**同一个** Lambda Layer，位于 `python/data/` 下。Lambda 函数分别通过 `/opt/python/data/GeoLite2-City.mmdb` 和 `/opt/python/data/GeoLite2-ASN.mmdb` 访问。
- ASN 与 City 共享 `USE_MAXMIND_DIRECT`、`MAXMIND_ACCOUNT_ID`、`MAXMIND_LICENSE_KEY`。使用 MaxMind 直连时仅版本 ID（`ASN_MAXMIND_EDITION_ID`）不同。
- 非 MaxMind 直连模式下，需设置 `ASN_DOWNLOAD_URL`。若未设置，ASN 下载会失败但不影响 City 更新（优雅降级）。
- 当目标 Layer 不存在时，首次推送会自动创建。

#### HASH_COMPARE_MODE

由于 ASN 数据库更新频率较高，可通过此变量控制哪种 hash 变化才触发 Layer 更新：

| 模式     | 行为                                     |
| -------- | ---------------------------------------- |
| `both` | City 或 ASN 任一 hash 变化即更新（默认） |
| `city` | 仅 City hash 变化触发更新，忽略 ASN 变化 |
| `asn`  | 仅 ASN hash 变化触发更新，忽略 City 变化 |
| `none` | 跳过 hash 比较，始终更新                 |

Layer 的 Description 采用紧凑格式存储 8 位短 hash 前缀，例如：`v20260916T08:00:20 c:39f45fa1 a:e4b2c8f1`（仅 City 时省略 `a:`）。切换模式无需重新推送，hash 始终写入 Description。

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

``docker run --env-file .env Claire9518/geoip-updater:latest --action check``

#### 2. 执行一次性更新

``docker run --env-file .env Claire9518/geoip-updater:latest --action update``

#### 3. 启动定时更新服务

``docker run -d --env-file .env Claire9518/geoip-updater:latest --action schedule``

### 日志查看

```
# 查看容器日志
docker logs geoip-updater

# 实时跟踪日志
docker logs -f geoip-updater
```
