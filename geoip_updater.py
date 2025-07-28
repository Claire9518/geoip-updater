#!/usr/bin/env python3
import boto3
import os
import tempfile
import zipfile
import tarfile
import glob
import shutil
import fcntl
import hashlib
import time
import sys
import signal
import subprocess
from datetime import datetime
import logging
import schedule
from dotenv import load_dotenv
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed

# 加载 .env 文件
load_dotenv(override=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='geoip_updater.log'
)

class GeoIPUpdater:
    def __init__(self):
        # 基础配置
        self.aws_profile = os.getenv('AWS_PROFILE', 'geoip-updater')
        self.layer_name = os.getenv('LAMBDA_LAYER_NAME', 'GeoLite2')
        
        # 区域配置
        regions_str = os.getenv('AWS_REGIONS', 'us-east-2')
        self.regions = [r.strip() for r in regions_str.split(',')]
        self.primary_region = os.getenv('PRIMARY_REGION', self.regions[0])
        if self.primary_region not in self.regions:
            self.primary_region = self.regions[0]
        
        # MaxMind 配置
        self.use_maxmind_direct = os.getenv('USE_MAXMIND_DIRECT', 'false').lower() == 'true'
        self.maxmind_account_id = os.getenv('MAXMIND_ACCOUNT_ID', '')
        self.maxmind_license_key = os.getenv('MAXMIND_LICENSE_KEY', '')
        self.maxmind_edition_id = os.getenv('MAXMIND_EDITION_ID', 'GeoLite2-City')
        self.maxmind_suffix = os.getenv('MAXMIND_SUFFIX', 'tar.gz')
        self.download_url = os.getenv('GEOIP_DOWNLOAD_URL', 
            'https://ghp.ci/https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb')
        
        # 网络配置
        self.connection_timeout = int(os.getenv('CONNECTION_TIMEOUT', '15'))
        self.max_time = int(os.getenv('MAX_DOWNLOAD_TIME', '60'))
        self.retry_count = int(os.getenv('RETRY_COUNT', '3'))
        
        # 锁文件和状态
        self.lock_file = '/tmp/geoip_updater.lock'
        self.lock_fd = None
        self.should_exit = False
        
        # 初始化 AWS 客户端
        self._init_aws_clients()
        
        # 设置信号处理器
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logging.info(f"初始化完成 - 主区域: {self.primary_region}, 所有区域: {', '.join(self.regions)}")
        logging.info(f"下载配置: {'MaxMind官方' if self.use_maxmind_direct else '备用链接'}")

    def _init_aws_clients(self):
        """初始化AWS客户端"""
        session = boto3.Session(profile_name=self.aws_profile)
        self.lambda_clients = {}
        for region in self.regions:
            self.lambda_clients[region] = session.client('lambda', region_name=region)
            logging.info(f"已初始化 {region} 区域的 Lambda 客户端")

    def _signal_handler(self, signum, frame):
        """信号处理器 - 优雅退出"""
        logging.info(f"接收到信号 {signum}，准备退出...")
        self.should_exit = True
        self._release_lock()
        sys.exit(0)

    def validate_environment(self, action='update'):
        """根据操作类型验证环境变量"""
        base_vars = {
            'AWS_PROFILE': self.aws_profile,
            'LAMBDA_LAYER_NAME': self.layer_name,
            'AWS_REGIONS': os.getenv('AWS_REGIONS')
        }
        
        action_vars = {
            'update': {
                'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID'),
                'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY')
            },
            'schedule': {
                'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID'),
                'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY'),
                'CRON_SCHEDULE': os.getenv('CRON_SCHEDULE', '0 2 * * *'),
                'TZ': os.getenv('TZ', 'UTC')
            }
        }
        
        # 合并所需变量
        required_vars = {**base_vars, **action_vars.get(action, {})}
        
        # 根据下载方式添加特定变量
        if action in ['update', 'schedule']:
            if self.use_maxmind_direct:
                required_vars.update({
                    'MAXMIND_ACCOUNT_ID': self.maxmind_account_id,
                    'MAXMIND_LICENSE_KEY': self.maxmind_license_key
                })
                if not self.maxmind_account_id or not self.maxmind_license_key:
                    raise EnvironmentError("使用 MaxMind 直接下载时必须提供 MAXMIND_ACCOUNT_ID 和 MAXMIND_LICENSE_KEY")
            else:
                required_vars['GEOIP_DOWNLOAD_URL'] = self.download_url
        
        # 检查缺失的变量
        missing_vars = [var for var, value in required_vars.items() if not value]
        if missing_vars:
            raise EnvironmentError(f"缺少必要的环境变量: {', '.join(missing_vars)}")
        
        logging.info(f"环境变量验证通过，操作: {action}")
        
        # 测试AWS权限
        if action in ['update', 'schedule']:
            self._test_aws_permissions()

    def _test_aws_permissions(self):
        """测试AWS权限"""
        for region in self.regions:
            try:
                lambda_client = self.lambda_clients[region]
                lambda_client.list_layer_versions(LayerName=self.layer_name)
                logging.info(f"区域 {region} 权限验证通过")
            except lambda_client.exceptions.ResourceNotFoundException:
                logging.info(f"区域 {region} 权限验证通过（Layer尚不存在）")
            except Exception as e:
                if 'is not authorized' in str(e):
                    raise PermissionError(f"区域 {region} 权限不足: {str(e)}")
                logging.warning(f"区域 {region} 权限测试遇到其他错误: {str(e)}")

    @contextmanager
    def file_lock(self):
        """文件锁上下文管理器"""
        if self._acquire_lock():
            try:
                yield
            finally:
                # 确保锁总是被释放
                self._release_lock()
        else:
            raise RuntimeError("无法获取更新锁，另一个进程可能正在运行")

    def _acquire_lock(self):
        """获取文件锁"""
        try:
            self.lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            logging.info("成功获取更新锁")
            return True
        except IOError:
            logging.info("另一个进程正在执行更新，跳过当前执行")
            if self.lock_fd:
                os.close(self.lock_fd)
                self.lock_fd = None
            return False
        except Exception as e:
            logging.error(f"获取锁失败: {str(e)}")
            return False

    def _release_lock(self):
        """释放文件锁"""
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
                self.lock_fd = None
                logging.info("已释放更新锁")
            except Exception as e:
                logging.error(f"释放锁失败: {str(e)}")

    def get_file_hash(self, filepath):
        """计算文件MD5哈希"""
        md5_hash = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()

    def verify_mmdb_file(self, file_path):
        """验证MMDB文件"""
        try:
            file_size = os.path.getsize(file_path)
            if file_size < 1024:
                raise ValueError("Downloaded file is too small")
            logging.info(f"MMDB file verified: {file_size} bytes")
            return True
        except Exception as e:
            logging.error(f"File verification failed: {str(e)}")
            return False

    def execute_with_retry(self, operation, max_retries=3, delay=5, *args, **kwargs):
        """通用重试机制"""
        for attempt in range(max_retries):
            try:
                return operation(*args, **kwargs)
            except Exception as e:
                logging.warning(f"操作失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                else:
                    raise

    def _build_curl_command(self, url, output_path, auth=None, extra_headers=None):
        """构建curl命令"""
        cmd = [
            'curl',
            '--location',
            '--fail',
            '--output', output_path,
            '--connect-timeout', str(self.connection_timeout),
            '--max-time', str(self.max_time),
            '--retry', str(self.retry_count),
            '--retry-delay', '2',
            '--progress-bar',
        ]
        
        # 添加认证
        if auth:
            cmd.extend(['--user', f'{auth[0]}:{auth[1]}'])
        
        # 添加额外头部
        if extra_headers:
            for header in extra_headers:
                cmd.extend(['--header', header])
        
        cmd.append(url)
        return cmd

    def _execute_curl(self, cmd, description="下载"):
        """执行curl命令并处理结果"""
        # 隐藏认证信息和AWS URL的日志
        safe_cmd = []
        skip_next = False
        for i, arg in enumerate(cmd):
            if skip_next:
                safe_cmd.append('[HIDDEN]')
                skip_next = False
            elif arg == '--user':
                safe_cmd.append(arg)
                skip_next = True
            elif arg.startswith('https://') and ('amazonaws.com' in arg or 'awslambda' in arg):
                safe_cmd.append('[AWS-URL-HIDDEN]')
            else:
                safe_cmd.append(arg)
        
        logging.info(f"执行{description}: {' '.join(safe_cmd)}")
        
        try:
            start_time = time.time()
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.max_time + 30,
                check=False
            )
            
            elapsed_time = time.time() - start_time
            
            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else f"curl返回码: {result.returncode}"
                raise subprocess.CalledProcessError(result.returncode, cmd, stderr=error_msg)
            
            logging.info(f"{description}完成，耗时: {elapsed_time:.2f}s")
            return result
            
        except subprocess.TimeoutExpired:
            logging.error(f"{description}超时 ({self.max_time + 30}s)")
            raise
        except subprocess.CalledProcessError as e:
            logging.error(f"{description}失败: {e.stderr}")
            raise
        except Exception as e:
            logging.error(f"{description}异常: {str(e)}")
            raise

    def download_mmdb(self):
        """下载MMDB数据库（统一入口）"""
        if self.use_maxmind_direct:
            return self.execute_with_retry(self._download_from_maxmind, max_retries=5)
        else:
            return self.execute_with_retry(self._download_from_backup, max_retries=3)

    def _download_from_maxmind(self):
        """从MaxMind官方下载"""
        logging.info(f"从 MaxMind 官方下载 {self.maxmind_edition_id}")
        
        maxmind_url = (
            f"https://download.maxmind.com/geoip/databases/{self.maxmind_edition_id}/"
            f"download?suffix={self.maxmind_suffix}"
        )
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"maxmind_{self.maxmind_edition_id}_{timestamp}.{self.maxmind_suffix}"
        downloaded_path = os.path.join('/tmp', filename)
        
        try:
            # 使用curl下载
            cmd = self._build_curl_command(
                url=maxmind_url,
                output_path=downloaded_path,
                auth=(self.maxmind_account_id, self.maxmind_license_key),
                extra_headers=['User-Agent: GeoIP-Updater/1.0', 'Accept: */*']
            )
            
            self._execute_curl(cmd, "MaxMind下载")
            
            # 验证下载结果
            if not os.path.exists(downloaded_path):
                raise FileNotFoundError(f"下载文件不存在: {downloaded_path}")
            
            file_size = os.path.getsize(downloaded_path)
            if file_size < 1024:
                os.unlink(downloaded_path)
                raise ValueError(f"下载文件太小: {file_size} bytes")
            
            logging.info(f"MaxMind 文件已下载: {downloaded_path}")
            
            # 如果是tar.gz，需要解压
            if self.maxmind_suffix == 'tar.gz':
                return self._extract_mmdb_from_targz(downloaded_path)
            return downloaded_path
            
        except Exception as e:
            logging.error(f"MaxMind下载失败: {str(e)}")
            if os.path.exists(downloaded_path):
                try:
                    os.unlink(downloaded_path)
                except:
                    pass
            raise

    def _download_from_backup(self):
        """从备用URL下载"""
        logging.info(f"从备用URL下载: {self.download_url}")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{timestamp}.mmdb"
        downloaded_path = os.path.join('/tmp', filename)
        
        try:
            # 使用curl下载
            cmd = self._build_curl_command(
                url=self.download_url,
                output_path=downloaded_path
            )
            
            self._execute_curl(cmd, "备用URL下载")
            
            # 验证文件
            if not os.path.exists(downloaded_path):
                raise FileNotFoundError(f"下载文件不存在: {downloaded_path}")
            
            file_size = os.path.getsize(downloaded_path)
            if file_size < 1024:
                os.unlink(downloaded_path)
                raise ValueError(f"下载文件太小: {file_size} bytes")
            
            logging.info(f"备用链接文件已下载: {downloaded_path}")
            return downloaded_path
            
        except Exception as e:
            logging.error(f"备用URL下载失败: {str(e)}")
            if os.path.exists(downloaded_path):
                try:
                    os.unlink(downloaded_path)
                except:
                    pass
            raise

    def _extract_mmdb_from_targz(self, targz_path):
        """从tar.gz提取MMDB文件"""
        logging.info(f"解压 tar.gz 文件: {targz_path}")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with tarfile.open(targz_path, 'r:gz') as tar:
                tar.extractall(temp_dir)
            
            # 递归查找MMDB文件
            mmdb_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith('.mmdb'):
                        mmdb_files.append(os.path.join(root, file))
            
            if not mmdb_files:
                raise FileNotFoundError("在解压文件中未找到 .mmdb 文件")
            
            # 选择匹配的文件或第一个
            source_mmdb = mmdb_files[0]
            for mmdb_file in mmdb_files:
                if self.maxmind_edition_id in os.path.basename(mmdb_file):
                    source_mmdb = mmdb_file
                    break
            
            if not self.verify_mmdb_file(source_mmdb):
                raise ValueError("解压的MMDB文件验证失败")
            
            # 复制到新临时文件
            with tempfile.NamedTemporaryFile(suffix='.mmdb', delete=False) as tmp_file:
                with open(source_mmdb, 'rb') as src:
                    shutil.copyfileobj(src, tmp_file)
                extracted_path = tmp_file.name
            
            logging.info(f"MMDB文件已提取: {extracted_path}")
            return extracted_path

    def create_layer_zip(self, mmdb_path):
        """创建Layer ZIP文件"""
        logging.info("创建 Layer ZIP 文件")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # 创建目录结构
            layer_dir = os.path.join(temp_dir, 'python/data')
            os.makedirs(layer_dir)
            
            # 复制MMDB文件
            dest_file = os.path.join(layer_dir, 'GeoLite2-City.mmdb')
            shutil.copy2(mmdb_path, dest_file)
            
            # 创建ZIP
            zip_path = os.path.join(temp_dir, 'layer.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, _, files in os.walk(os.path.join(temp_dir, 'python')):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arc_name = os.path.relpath(file_path, temp_dir)
                        zip_file.write(file_path, arc_name)
            
            # 读取ZIP内容
            with open(zip_path, 'rb') as f:
                return f.read()

    def execute_lambda_operation(self, operation_name, region, **kwargs):
        """执行Lambda操作的统一接口"""
        lambda_client = self.lambda_clients[region]
        operation = getattr(lambda_client, operation_name)
        return operation(**kwargs)

    def get_layer_info(self, region=None):
        """获取Layer信息"""
        region = region or self.primary_region
        try:
            response = self.execute_lambda_operation('list_layer_versions', region, LayerName=self.layer_name)
            versions = response.get('LayerVersions', [])
            if versions:
                latest = versions[0]
                return {
                    'version': latest['Version'],
                    'created_date': latest['CreatedDate'],
                    'arn': latest['LayerVersionArn'],
                    'region': region
                }
            return None
        except Exception as e:
            logging.warning(f"区域 {region} 获取Layer信息失败: {str(e)}")
            return None

    def check_update_needed(self, mmdb_path, region=None):
        """检查是否需要更新"""
        region = region or self.primary_region
        layer_info = self.get_layer_info(region)
        
        if not layer_info:
            logging.info(f"区域 {region} 没有现有Layer，需要更新")
            return True
        
        try:
            # 获取现有Layer的详细信息
            response = self.execute_lambda_operation(
                'get_layer_version', region,
                LayerName=self.layer_name,
                VersionNumber=layer_info['version']
            )
            
            download_url = response.get('Content', {}).get('Location')
            if not download_url:
                logging.warning(f"区域 {region} 无法获取Layer下载URL，需要更新")
                return True
            
            # 下载并比较现有Layer
            return self._compare_layer_content(mmdb_path, download_url, region)
            
        except Exception as e:
            logging.warning(f"区域 {region} 比较Layer失败: {str(e)}")
            return True

    def _compare_layer_content(self, new_mmdb_path, download_url, region):
        """比较Layer内容"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # 使用curl下载现有Layer
            current_layer_path = os.path.join(temp_dir, 'current_layer.zip')
            
            cmd = self._build_curl_command(
                url=download_url,
                output_path=current_layer_path
            )
            
            try:
                self._execute_curl(cmd, f"区域{region}现有Layer下载")
            except Exception as e:
                logging.warning(f"区域 {region} 下载现有Layer失败: {str(e)}，需要更新")
                return True
            
            # 解压并找到MMDB文件
            current_mmdb_path = os.path.join(temp_dir, 'python/data/GeoLite2-City.mmdb')
            try:
                with zipfile.ZipFile(current_layer_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
            except Exception as e:
                logging.warning(f"区域 {region} 解压现有Layer失败: {str(e)}，需要更新")
                return True
            
            if not os.path.exists(current_mmdb_path):
                logging.warning(f"区域 {region} 现有Layer中未找到MMDB文件，需要更新")
                return True
            
            # 比较文件哈希
            new_hash = self.get_file_hash(new_mmdb_path)
            current_hash = self.get_file_hash(current_mmdb_path)
            
            if new_hash != current_hash:
                new_size = os.path.getsize(new_mmdb_path)
                current_size = os.path.getsize(current_mmdb_path)
                logging.info(f"区域 {region} 文件内容有变化，大小差异: {abs(new_size - current_size)} bytes")
                return True
            
            logging.info(f"区域 {region} 文件内容相同，无需更新")
            return False

    def update_layer_version(self, zip_content, region):
        """更新单个区域的Layer版本"""
        try:
            response = self.execute_lambda_operation(
                'publish_layer_version', region,
                LayerName=self.layer_name,
                Description=f'GeoIP database updated at {datetime.now().isoformat()} for {region}',
                Content={'ZipFile': zip_content},
                CompatibleRuntimes=['python3.8', 'python3.9', 'python3.10', 'python3.12'],
                CompatibleArchitectures=['x86_64', 'arm64']
            )
            
            logging.info(f"区域 {region} Layer更新成功，版本: {response['Version']}")
            return {
                'status': 'success',
                'version': response['Version'],
                'arn': response['LayerVersionArn']
            }
        except Exception as e:
            logging.error(f"区域 {region} Layer更新失败: {str(e)}")
            return {'status': 'failed', 'error': str(e)}

    def update_functions_using_layer(self, layer_version_arn, region):
        """更新使用指定Layer的所有函数"""
        try:
            updated_functions = []
            paginator = self.lambda_clients[region].get_paginator('list_functions')
            
            for page in paginator.paginate():
                for function in page['Functions']:
                    function_name = function['FunctionName']
                    
                    # 获取函数配置
                    config = self.execute_lambda_operation(
                        'get_function_configuration', region,
                        FunctionName=function_name
                    )
                    
                    # 检查并更新Layer
                    if self._update_function_layer(function_name, layer_version_arn, config, region):
                        updated_functions.append(function_name)
            
            logging.info(f"区域 {region} 已更新 {len(updated_functions)} 个函数的Layer版本")
            return updated_functions
            
        except Exception as e:
            logging.error(f"区域 {region} 更新函数Layer失败: {str(e)}")
            return []

    def _update_function_layer(self, function_name, new_layer_arn, config, region):
        """更新单个函数的Layer配置"""
        current_layers = config.get('Layers', [])
        new_layers = []
        has_geolite2 = False
        
        for layer in current_layers:
            if f':layer:{self.layer_name}:' in layer['Arn']:
                new_layers.append(new_layer_arn)
                has_geolite2 = True
                logging.info(f"区域 {region} 函数 {function_name} 更新Layer: {layer['Arn']} -> {new_layer_arn}")
            else:
                new_layers.append(layer['Arn'])
        
        if not has_geolite2:
            return False
        
        try:
            self.execute_lambda_operation(
                'update_function_configuration', region,
                FunctionName=function_name,
                Layers=new_layers
            )
            return True
        except Exception as e:
            logging.error(f"区域 {region} 更新函数 {function_name} 配置失败: {str(e)}")
            return False

    def cleanup_old_layer_versions(self, region, keep_latest_n=2):
        """清理旧的Layer版本"""
        try:
            response = self.execute_lambda_operation('list_layer_versions', region, LayerName=self.layer_name)
            versions = response.get('LayerVersions', [])
            
            if len(versions) <= keep_latest_n:
                return
            
            versions.sort(key=lambda x: x['Version'], reverse=True)
            versions_to_check = versions[keep_latest_n:]
            
            for version in versions_to_check:
                version_arn = version['LayerVersionArn']
                version_number = version['Version']
                
                # 检查是否有函数在使用
                if not self._is_layer_version_in_use(version_arn, region):
                    try:
                        self.execute_lambda_operation(
                            'delete_layer_version', region,
                            LayerName=self.layer_name,
                            VersionNumber=version_number
                        )
                        logging.info(f"区域 {region} 已删除未使用的Layer版本 {version_number}")
                    except Exception as e:
                        logging.error(f"区域 {region} 删除Layer版本 {version_number} 失败: {str(e)}")
        
        except Exception as e:
            logging.error(f"区域 {region} 清理Layer版本失败: {str(e)}")

    def _is_layer_version_in_use(self, layer_version_arn, region):
        """检查Layer版本是否被使用"""
        try:
            paginator = self.lambda_clients[region].get_paginator('list_functions')
            for page in paginator.paginate():
                for function in page['Functions']:
                    config = self.execute_lambda_operation(
                        'get_function_configuration', region,
                        FunctionName=function['FunctionName']
                    )
                    if 'Layers' in config:
                        if any(layer['Arn'] == layer_version_arn for layer in config['Layers']):
                            return True
            return False
        except Exception:
            return True  # 如果检查失败，保守起见认为在使用

    def cleanup_temp_files(self, after_update=False):
        """清理临时文件"""
        current_time = time.time()
        age_threshold = 3600 if after_update else 24 * 3600  # 1小时或24小时
        
        patterns = [
            ('/tmp/*.mmdb', 'MMDB文件'),
            ('/tmp/*.tar.gz', 'tar.gz文件'),
            ('/tmp/maxmind_*', 'MaxMind文件'),
            ('/tmp/backup_*', '备用文件')
        ]
        
        for pattern, file_type in patterns:
            self._cleanup_files_by_pattern(pattern, age_threshold, file_type)
        
        # 清理临时目录
        self._cleanup_temp_directories(age_threshold)

    def _cleanup_files_by_pattern(self, pattern, age_threshold, file_type):
        """按模式清理文件"""
        files = glob.glob(pattern)
        current_time = time.time()
        
        for file_path in files:
            try:
                if current_time - os.path.getmtime(file_path) > age_threshold:
                    os.remove(file_path)
                    logging.info(f"已删除过期{file_type}: {file_path}")
            except Exception as e:
                logging.warning(f"删除{file_type} {file_path} 失败: {str(e)}")

    def _cleanup_temp_directories(self, age_threshold):
        """清理临时目录"""
        temp_patterns = ['/tmp/tmp*', '/tmp/GeoLite2-*', '/tmp/GeoIP2-*']
        current_time = time.time()
        
        for pattern in temp_patterns:
            for dir_path in glob.glob(pattern):
                try:
                    if os.path.isdir(dir_path):
                        if current_time - os.path.getmtime(dir_path) > age_threshold:
                            shutil.rmtree(dir_path)
                            logging.info(f"已删除过期目录: {dir_path}")
                except Exception as e:
                    logging.warning(f"删除目录 {dir_path} 失败: {str(e)}")

    def update_all_regions(self, mmdb_path):
        """更新所有区域"""
        zip_content = self.create_layer_zip(mmdb_path)
        results = {}
        
        # 使用线程池并行更新
        with ThreadPoolExecutor(max_workers=min(len(self.regions), 5)) as executor:
            future_to_region = {
                executor.submit(self._update_single_region, region, zip_content): region
                for region in self.regions
            }
            
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    results[region] = future.result()
                except Exception as e:
                    logging.error(f"区域 {region} 更新异常: {str(e)}")
                    results[region] = {'status': 'failed', 'error': str(e)}
        
        self._log_update_results(results)
        return results

    def _update_single_region(self, region, zip_content):
        """更新单个区域的完整流程"""
        try:
            # 更新Layer版本
            layer_result = self.update_layer_version(zip_content, region)
            if layer_result['status'] != 'success':
                return layer_result
            
            # 更新函数
            updated_functions = self.update_functions_using_layer(layer_result['arn'], region)
            
            # 清理旧版本
            self.cleanup_old_layer_versions(region)
            
            layer_result['updated_functions'] = len(updated_functions)
            return layer_result
            
        except Exception as e:
            logging.error(f"区域 {region} 更新失败: {str(e)}")
            return {'status': 'failed', 'error': str(e)}

    def _log_update_results(self, results):
        """记录更新结果"""
        logging.info("=== 多区域更新结果摘要 ===")
        success_count = sum(1 for r in results.values() if r['status'] == 'success')
        failed_count = sum(1 for r in results.values() if r['status'] == 'failed')
        skipped_count = sum(1 for r in results.values() if r['status'] == 'skipped')
        
        for region, result in results.items():
            if result['status'] == 'success':
                functions = result.get('updated_functions', 0)
                logging.info(f"✓ {region}: 成功更新到版本 {result['version']}, 更新了 {functions} 个函数")
            elif result['status'] == 'skipped':
                logging.info(f"- {region}: 跳过更新 ({result.get('reason', 'unknown')})")
            else:
                logging.error(f"✗ {region}: 更新失败 - {result.get('error', 'unknown')}")
        
        logging.info(f"=== 总计: 成功 {success_count}, 跳过 {skipped_count}, 失败 {failed_count} ===")

    def update_layer(self, force_update=False):
        """主更新方法"""
        try:
            with self.file_lock():
                # 清理旧文件
                self.cleanup_temp_files(after_update=False)
                
                try:
                    # 下载数据库
                    mmdb_path = self.download_mmdb()
                    logging.info(f"成功下载数据库: {mmdb_path}")
                    
                    # 检查是否需要更新
                    if not force_update and not self.check_update_needed(mmdb_path, self.primary_region):
                        logging.info("主区域无需更新，跳过所有区域")
                        results = {region: {'status': 'skipped', 'reason': 'no_update_needed'} 
                                for region in self.regions}
                        self._log_update_results(results)
                        
                        # 清理下载的文件
                        os.unlink(mmdb_path)
                        self.cleanup_temp_files(after_update=True)
                        return results
                    
                    # 执行更新
                    results = self.update_all_regions(mmdb_path)
                    
                    # 清理临时文件
                    os.unlink(mmdb_path)
                    self.cleanup_temp_files(after_update=True)
                    
                    return results
                    
                except Exception as e:
                    logging.error(f"下载或更新过程失败: {str(e)}")
                    self.cleanup_temp_files(after_update=True)
                    raise
                    
        except KeyboardInterrupt:
            logging.info("检测到手动中断，程序退出")
            raise
        except Exception as e:
            logging.error(f"更新操作失败: {str(e)}")
            raise

def update_job():
    """定时任务执行函数"""
    logging.info("开始执行定时更新任务")
    updater = GeoIPUpdater()
    try:
        updater.update_layer(force_update=False)
        logging.info("定时更新任务完成")
    except Exception as e:
        logging.error(f"定时更新任务失败: {str(e)}")
    finally:
        updater.cleanup_temp_files(after_update=False)

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='GeoIP Database Updater')
    parser.add_argument('--action', 
                       choices=['update', 'check', 'schedule', 'cleanup'],
                       default='update',
                       help='操作类型')
    parser.add_argument('--force', action='store_true', help='强制更新')
    parser.add_argument('--cleanup-mode', 
                       choices=['normal', 'after-update'],
                       default='normal',
                       help='清理模式')
    
    args = parser.parse_args()

    if args.action == 'update':
        logging.info("开始执行GeoIP数据库更新...")
    
    try:
        updater = GeoIPUpdater()
        updater.validate_environment(args.action)
        
        if args.action == 'cleanup':
            updater.cleanup_temp_files(after_update=(args.cleanup_mode == 'after-update'))
            
        elif args.action == 'update':
            updater.update_layer(force_update=args.force)
            
        elif args.action == 'check':
            for region in updater.regions:
                info = updater.get_layer_info(region)
                if info:
                    logging.info(f"区域 {region}: 版本 {info['version']}, 更新时间 {info['created_date']}")
                else:
                    logging.info(f"区域 {region}: 未找到Layer")
                    
        elif args.action == 'schedule':
            logging.info("启动定时任务...")
            schedule.every().day.at("02:00").do(update_job)
            update_job()  # 首次执行
            
            while True:
                schedule.run_pending()
                time.sleep(60)
                if updater.should_exit:
                    break
                    
    except KeyboardInterrupt:
        logging.info("用户中断，程序退出")
        sys.exit(0)
    except Exception as e:
        logging.error(f"程序执行失败: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()