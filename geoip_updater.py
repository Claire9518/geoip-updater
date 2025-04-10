#!/usr/bin/env python3
import boto3
import requests
import os
import tempfile
import zipfile
import glob
import shutil
import fcntl
from datetime import datetime
import logging
import schedule
import time
import sys
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()  # 默认加载当前目录下的 .env 文件
# 或者指定 .env 文件路径
# load_dotenv('/app/.env')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='geoip_updater.log'
)

class GeoIPUpdater:
    def __init__(self):
        # 从环境变量获取配置
        self.aws_profile = os.getenv('AWS_PROFILE', 'geoip-updater')
        self.layer_name = os.getenv('LAMBDA_LAYER_NAME', 'GeoLite2')
        self.region = os.getenv('AWS_REGION', 'us-east-2')
        self.download_url = os.getenv('GEOIP_DOWNLOAD_URL', 
            'https://ghp.ci/https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb')
        self.lock_file = '/tmp/geoip_updater.lock'
        
        # 初始化 AWS 客户端
        session = boto3.Session(profile_name=self.aws_profile)
        self.lambda_client = session.client('lambda', region_name=self.region)

    def verify_mmdb_file(self, file_path):
        try:
            # 检查文件大小
            file_size = os.path.getsize(file_path)
            if file_size < 1024:  # 小于 1KB 可能是错误文件
                raise ValueError("Downloaded file is too small")
                
            # 尝试读取文件头部
            with open(file_path, 'rb') as f:
                header = f.read(16)
                if not header.startswith(b'\xab\xcd\xefMaxMind.com'):
                    raise ValueError("Invalid MMDB file format")
            
            logging.info(f"MMDB file verified: {file_size} bytes")
            return True
        except Exception as e:
            logging.error(f"File verification failed: {str(e)}")
            return False
    
    def validate_environment(self):
        """验证必需的环境变量"""
        required_vars = {
            'AWS_PROFILE': self.aws_profile,
            'AWS_REGION': self.region,
            'LAMBDA_LAYER_NAME': self.layer_name,
            'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID'),
            'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY'),
            'GEOIP_DOWNLOAD_URL': self.download_url
        }
        
        missing_vars = [var for var, value in required_vars.items() if not value]
        if missing_vars:
            raise EnvironmentError(f"缺少必要的环境变量: {', '.join(missing_vars)}")
            
        logging.info("环境变量验证通过")

    def download_mmdb(self):
        """下载 MaxMind 数据库"""
        logging.info(f"开始从 {self.download_url} 下载 GeoIP 数据库")
        
        try:
            response = requests.get(self.download_url, timeout=30)
            response.raise_for_status()
            
            # 直接保存为 mmdb 文件
            with tempfile.NamedTemporaryFile(suffix='.mmdb', delete=False) as tmp_file:
                tmp_file.write(response.content)
                tmp_path = tmp_file.name
                logging.info(f"文件已下载到: {tmp_path}")
                return tmp_path
        except Exception as e:
            logging.error(f"下载失败: {str(e)}")
            raise

    def create_layer_zip(self, mmdb_path):
        """创建 Layer ZIP 文件"""
        logging.info("创建 Layer ZIP 文件")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # 创建目录结构
            layer_dir = os.path.join(temp_dir, 'python/data')
            os.makedirs(layer_dir)
            
            # 复制 mmdb 文件
            dest_file = os.path.join(layer_dir, 'GeoLite2-City.mmdb')
            with open(mmdb_path, 'rb') as src, open(dest_file, 'wb') as dst:
                dst.write(src.read())
            
            # 创建 zip 文件
            zip_path = os.path.join(temp_dir, 'layer.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, _, files in os.walk(os.path.join(temp_dir, 'python')):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arc_name = os.path.relpath(file_path, os.path.join(temp_dir))
                        zip_file.write(file_path, arc_name)
            
            # 读取 zip 文件
            with open(zip_path, 'rb') as f:
                return f.read()

    def update_function_layer(self, function_name, layer_version_arn):
        """更新 Lambda 函数的层版本"""
        try:
            # 获取当前函数配置
            response = self.lambda_client.get_function_configuration(
                FunctionName=function_name
            )
            
            # 获取当前的层配置
            current_layers = response.get('Layers', [])
            logging.info(f"当前层配置: {current_layers}")
            
            # 检查是否有 GeoLite2 层
            has_geolite2 = False
            
            # 创建新的层列表，只更新 GeoLite2 层，保持其他层不变
            new_layers = []
            for layer in current_layers:
                current_arn = layer['Arn']
                # 使用更精确的匹配方式
                if ':layer:GeoLite2:' in current_arn:
                    # 更新 GeoLite2 层
                    logging.info(f"找到 GeoLite2 层，更新版本: {current_arn} -> {layer_version_arn}")
                    new_layers.append(layer_version_arn)
                    has_geolite2 = True
                else:
                    # 保持其他层的 ARN 不变
                    logging.info(f"保持其他层不变: {current_arn}")
                    new_layers.append(current_arn)
            
            if not has_geolite2:
                logging.info(f"函数 {function_name} 没有使用 GeoLite2 层，跳过更新")
                return None
                
            if new_layers:
                logging.info(f"准备更新函数配置，新的层列表: {new_layers}")
                try:
                    response = self.lambda_client.update_function_configuration(
                        FunctionName=function_name,
                        Layers=new_layers
                    )
                    logging.info(f"已更新函数 {function_name} 的层配置")
                    return response
                except Exception as e:
                    logging.error(f"更新函数配置时出错: {str(e)}")
                    logging.error(f"尝试更新的层配置: {new_layers}")
                    raise
            
            return None
                    
        except Exception as e:
            logging.error(f"处理函数层版本更新失败: {str(e)}")
            raise

    def update_all_functions_using_layer(self, layer_version_arn):
        """更新所有使用该层的函数"""
        try:
            # 列出所有函数
            paginator = self.lambda_client.get_paginator('list_functions')
            for page in paginator.paginate():
                for function in page['Functions']:
                    function_name = function['FunctionName']
                    
                    # 获取函数配置
                    config = self.lambda_client.get_function_configuration(
                        FunctionName=function_name
                    )
                    
                    # 检查是否使用了这个层
                    if 'Layers' in config:
                        for layer in config['Layers']:
                            if self.layer_name in layer['Arn']:
                                logging.info(f"更新函数 {function_name} 的层版本")
                                self.update_function_layer(function_name, layer_version_arn)
                                break
            
            logging.info("已完成所有函数的层版本更新")
            
        except Exception as e:
            logging.error(f"更新函数层版本失败: {str(e)}")
            raise

    def check_layer_status(self):
        """检查 Layer 状态并返回最新的版本信息"""
        try:
            response = self.lambda_client.list_layer_versions(
                LayerName=self.layer_name
            )
            versions = response.get('LayerVersions', [])
            if versions:
                latest = versions[0]
                logging.info(f"当前 Layer 版本: {latest['Version']}")
                logging.info(f"更新时间: {latest['CreatedDate']}")
                return {
                    'version': latest['Version'],
                    'created_date': latest['CreatedDate'],
                    'arn': latest['LayerVersionArn']
                }
            return None
        except Exception as e:
            logging.warning(f"获取现有 Layer 信息失败: {str(e)}")
            return None

    def check_mmdb_update_needed(self, mmdb_path):
        """检查是否需要更新 MMDB"""
        try:
            # 获取当前 Layer 的信息
            layer_info = self.check_layer_status()
            if not layer_info:
                logging.info("没有找到现有 Layer，需要更新")
                return True

            # 下载现有的 Layer 版本进行比较
            try:
                # 获取最新版本的详细信息
                response = self.lambda_client.get_layer_version(
                    LayerName=self.layer_name,
                    VersionNumber=layer_info['version']
                )
                
                # 获取当前 Layer 的下载 URL
                download_url = response.get('Content', {}).get('Location')
                if not download_url:
                    logging.warning("无法获取现有 Layer 的下载 URL，需要更新")
                    return True
                
                # 下载现有的 Layer
                with tempfile.TemporaryDirectory() as temp_dir:
                    current_layer_path = os.path.join(temp_dir, 'current_layer.zip')
                    response = requests.get(download_url)
                    with open(current_layer_path, 'wb') as f:
                        f.write(response.content)
                    
                    # 解压现有的 Layer
                    current_mmdb_path = os.path.join(temp_dir, 'python/data/GeoLite2-City.mmdb')
                    with zipfile.ZipFile(current_layer_path, 'r') as zip_ref:
                        zip_ref.extractall(temp_dir)
                    
                    # 比较文件
                    if not os.path.exists(current_mmdb_path):
                        logging.warning("现有 Layer 中未找到 MMDB 文件，需要更新")
                        return True
                    
                    # 比较文件大小
                    new_size = os.path.getsize(mmdb_path)
                    current_size = os.path.getsize(current_mmdb_path)
                    size_diff = abs(new_size - current_size)
                    
                    # 比较文件内容（使用 MD5 哈希）
                    import hashlib
                    
                    def get_file_hash(filepath):
                        md5_hash = hashlib.md5()
                        with open(filepath, "rb") as f:
                            for chunk in iter(lambda: f.read(4096), b""):
                                md5_hash.update(chunk)
                        return md5_hash.hexdigest()
                    
                    new_hash = get_file_hash(mmdb_path)
                    current_hash = get_file_hash(current_mmdb_path)
                    
                    if new_hash != current_hash:
                        # 如果哈希值不同且大小差异超过 1KB
                        if size_diff > 1024:
                            logging.info(f"文件内容有显著变化：")
                            logging.info(f"大小差异: {size_diff} bytes (新: {new_size}, 现有: {current_size})")
                            logging.info(f"哈希值不同: (新: {new_hash}, 现有: {current_hash})")
                            return True
                        else:
                            logging.info("文件内容有变化但差异很小，可能是小幅更新")
                            return True
                    
                    logging.info("文件内容完全相同，无需更新")
                    return False
                    
            except Exception as e:
                logging.warning(f"比较文件失败: {str(e)}")
                return True

        except Exception as e:
            logging.error(f"检查更新失败: {str(e)}")
            return True
        
    def cleanup_tmp_files(self, after_update=False):
        """清理临时文件"""
        try:
            current_time = time.time()
            # 如果是更新后清理，使用较短的时间阈值
            age_threshold = 1 * 3600 if after_update else 24 * 3600  # 更新后1小时，否则24小时
            
            # 清理 .mmdb 文件
            mmdb_pattern = os.path.join('/tmp', '*.mmdb')
            mmdb_files = glob.glob(mmdb_pattern)
            
            if after_update:
                # 更新后清理：保留最新的两个文件（可能需要用于比对）
                if len(mmdb_files) > 2:
                    # 按修改时间排序
                    mmdb_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                    # 保留最新的两个文件，删除其他的
                    for file in mmdb_files[2:]:
                        try:
                            os.remove(file)
                            logging.info(f"更新后清理 - 已删除旧的临时文件: {file}")
                        except Exception as e:
                            logging.warning(f"删除文件 {file} 失败: {str(e)}")
            else:
                # 常规清理：删除超过时间阈值的文件
                for file in mmdb_files:
                    try:
                        file_age = current_time - os.path.getmtime(file)
                        if file_age > age_threshold:
                            os.remove(file)
                            logging.info(f"常规清理 - 已删除过期临时文件: {file}")
                    except Exception as e:
                        logging.warning(f"删除文件 {file} 失败: {str(e)}")

            # 清理临时目录，但只清理明显是旧的目录
            tmp_pattern = os.path.join('/tmp', 'tmp*')
            for dir_path in glob.glob(tmp_pattern):
                try:
                    if os.path.isdir(dir_path):
                        dir_age = current_time - os.path.getmtime(dir_path)
                        # 目录清理使用较短的时间阈值
                        dir_threshold = 2 * 3600  # 2小时
                        if dir_age > dir_threshold:
                            shutil.rmtree(dir_path)
                            logging.info(f"已删除旧的临时目录: {dir_path}")
                except Exception as e:
                    logging.warning(f"删除目录 {dir_path} 失败: {str(e)}")

        except Exception as e:
            logging.error(f"清理临时文件失败: {str(e)}")
    def list_functions_using_layer_version(self, layer_version_arn):
        """列出使用指定层版本的所有函数"""
        functions = []
        try:
            paginator = self.lambda_client.get_paginator('list_functions')
            for page in paginator.paginate():
                for function in page['Functions']:
                    function_name = function['FunctionName']
                    config = self.lambda_client.get_function_configuration(
                        FunctionName=function_name
                    )
                    if 'Layers' in config:
                        if any(layer['Arn'] == layer_version_arn for layer in config['Layers']):
                            functions.append(function_name)
        except Exception as e:
            logging.error(f"获取使用层版本的函数列表失败: {str(e)}")
        return functions

    def cleanup_unused_layer_versions(self, keep_latest_n=2):
        """清理未使用的层版本
            keep_latest_n (int): 保留最新的几个版本（即使未被使用）
        """
        try:
            # 获取所有层版本
            response = self.lambda_client.list_layer_versions(
                LayerName=self.layer_name
            )
            versions = response.get('LayerVersions', [])
            
            if not versions:
                logging.info(f"没有找到 {self.layer_name} 的任何版本")
                return
            
            # 按版本号降序排序
            versions.sort(key=lambda x: x['Version'], reverse=True)
            
            # 保留最新的 N 个版本
            versions_to_keep = versions[:keep_latest_n]
            versions_to_check = versions[keep_latest_n:]
            
            logging.info(f"保留最新的 {keep_latest_n} 个版本：" + 
                        ", ".join([str(v['Version']) for v in versions_to_keep]))
            
            # 检查每个可能删除的版本
            for version in versions_to_check:
                version_arn = version['LayerVersionArn']
                version_number = version['Version']
                
                # 检查是否有函数在使用这个版本
                functions = self.list_functions_using_layer_version(version_arn)
                
                if functions:
                    logging.info(f"层版本 {version_number} 正在被以下函数使用，跳过删除：" + 
                            ", ".join(functions))
                    continue
                
                try:
                    # 删除未使用的版本
                    self.lambda_client.delete_layer_version(
                        LayerName=self.layer_name,
                        VersionNumber=version_number
                    )
                    logging.info(f"已删除未使用的层版本 {version_number}")
                except Exception as e:
                    logging.error(f"删除层版本 {version_number} 失败: {str(e)}")
            
            logging.info("层版本清理完成")
            
        except Exception as e:
            logging.error(f"清理未使用的层版本失败: {str(e)}")

    def acquire_lock(self):
        """获取文件锁以确保只有一个进程在执行更新"""
        try:
            # 创建锁文件（如果不存在）
            lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)
            
            # 尝试获取独占锁，非阻塞模式
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                logging.info("成功获取更新锁")
                return lock_fd
            except IOError:
                # 如果锁已被其他进程持有
                logging.info("另一个进程正在执行更新，跳过当前执行")
                os.close(lock_fd)
                return None
        except Exception as e:
            logging.error(f"获取锁失败: {str(e)}")
            return None

    def release_lock(self, lock_fd):
        """释放文件锁"""
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                logging.info("已释放更新锁")
            except Exception as e:
                logging.error(f"释放锁失败: {str(e)}")
                
    def update_layer(self):
        """更新 Lambda Layer"""
        max_retries = 3
        retry_delay = 60  # 秒
        lock_fd = None
        
        try:
            # 尝试获取锁
            lock_fd = self.acquire_lock()
            if lock_fd is None:
                return None  # 无法获取锁，返回
                
            # 执行常规清理
            self.cleanup_tmp_files(after_update=False)
            
            for attempt in range(max_retries):
                try:
                    # 下载数据库
                    mmdb_path = self.download_mmdb()
                    logging.info(f"成功下载数据库到: {mmdb_path}")
                    
                    # 检查是否需要更新
                    if not self.check_mmdb_update_needed(mmdb_path):
                        logging.info("MMDB 文件无需更新")
                        os.unlink(mmdb_path)
                        return None
                    
                    file_size = os.path.getsize(mmdb_path)
                    logging.info(f"下载的文件大小: {file_size} bytes")
                    
                    zip_content = self.create_layer_zip(mmdb_path)
                    logging.info("成功创建 Layer ZIP 文件")
                    
                    response = self.lambda_client.publish_layer_version(
                        LayerName=self.layer_name,
                        Description=f'GeoIP database updated at {datetime.now().isoformat()}',
                        Content={
                            'ZipFile': zip_content
                        },
                        CompatibleRuntimes=['python3.8', 'python3.9', 'python3.10', 'python3.12'],
                        CompatibleArchitectures=['x86_64', 'arm64']
                    )
                    
                    new_version_arn = response['LayerVersionArn']
                    logging.info(f"Layer 更新成功，新版本: {response['Version']}")
                    
                    # 更新使用该层的所有函数
                    logging.info("开始更新使用该层的函数...")
                    self.update_all_functions_using_layer(new_version_arn)
                    
                    # 清理临时文件
                    os.unlink(mmdb_path)

                    self.cleanup_tmp_files(after_update=True)
                    # 在成功更新后添加清理操作
                    logging.info("开始清理未使用的层版本...")
                    self.cleanup_unused_layer_versions(keep_latest_n=2)
                    return response['Version']
                    
                except Exception as e:
                    logging.error(f"更新失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                    if attempt < max_retries - 1:
                        logging.info(f"等待 {retry_delay} 秒后重试...")
                        time.sleep(retry_delay)
                    else:
                        # 最后一次尝试也失败了，执行清理
                        self.cleanup_tmp_files(after_update=True)
                        raise
                        
        finally:
            # 确保释放锁
            self.release_lock(lock_fd)

def update_job():
    """定时任务"""
    logging.info("开始执行更新任务")
    updater = GeoIPUpdater()
    try:
        updater.update_layer()
        logging.info("更新任务完成")
    except Exception as e:
        logging.error(f"更新任务失败: {str(e)}")
    finally:
        # 在定时任务结束时执行常规清理
        updater.cleanup_tmp_files(after_update=False)

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='GeoIP Database Updater')
    parser.add_argument('--action', 
                       choices=['update', 'check', 'schedule', 'test-update', 'test-function', 'cleanup'],
                       default='update',
                       help='选择操作: update=执行更新, check=检查状态, schedule=启动定时任务, test-update=测试更新层, test-function=测试函数更新, cleanup=清理临时文件')
    parser.add_argument('--function-name',
                       help='指定要测试的函数名称（与 test-function 一起使用）')
    parser.add_argument('--cleanup-mode',
                       choices=['normal', 'after-update'],
                       default='normal',
                       help='清理模式：normal=常规清理，after-update=更新后清理')
    
    args = parser.parse_args()
    
    try:
        # 创建更新器实例
        updater = GeoIPUpdater()
        
        # 基础环境变量验证
        required_vars = {
            'AWS_PROFILE': os.getenv('AWS_PROFILE'),
            'AWS_REGION': os.getenv('AWS_REGION'),
            'LAMBDA_LAYER_NAME': os.getenv('LAMBDA_LAYER_NAME')
        }
        
        # 根据不同的操作添加额外的必需环境变量
        if args.action in ['update', 'schedule', 'test-update']:
            required_vars.update({
                'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID'),
                'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY'),
                'GEOIP_DOWNLOAD_URL': os.getenv('GEOIP_DOWNLOAD_URL')
            })
            
            if args.action == 'schedule':
                required_vars.update({
                    'CRON_SCHEDULE': os.getenv('CRON_SCHEDULE'),
                    'TZ': os.getenv('TZ')
                })
        
        # 验证环境变量
        missing_vars = [var for var, value in required_vars.items() if not value]
        if missing_vars:
            raise EnvironmentError(f"缺少必要的环境变量: {', '.join(missing_vars)}")
        
        logging.info("环境变量验证通过")
        
        # 执行相应的操作
        if args.action == 'cleanup':
            # 执行清理
            logging.info(f"执行临时文件清理 (模式: {args.cleanup_mode})...")
            updater.cleanup_tmp_files(after_update=(args.cleanup_mode == 'after-update'))
            
        elif args.action == 'update':
            # 执行单次更新
            logging.info("开始执行更新...")
            version = updater.update_layer()
            logging.info(f"更新成功完成！新版本: {version}")
            
        elif args.action == 'check':
            # 只检查状态
            logging.info("检查 Layer 状态...")
            updater.check_layer_status()
            
        elif args.action == 'schedule':
            # 启动定时任务
            logging.info("启动定时任务...")
                
            schedule.every().day.at("02:00").do(update_job)
            update_job()  # 首次运行
            while True:
                schedule.run_pending()
                time.sleep(60)
        
        elif args.action == 'test-update':
            # 测试更新层功能
            logging.info("测试更新层功能...")
            layer_info = updater.check_layer_status()
            if layer_info:
                logging.info(f"当前层信息: Version={layer_info['version']}, ARN={layer_info['arn']}")
                # 获取函数列表进行测试
                try:
                    response = updater.lambda_client.list_functions()
                    for function in response['Functions']:
                        logging.info(f"发现函数: {function['FunctionName']}")
                        if 'Layers' in function:
                            logging.info(f"函数 {function['FunctionName']} 的现有层配置: {function['Layers']}")
                except Exception as e:
                    logging.error(f"获取函数列表失败: {str(e)}")
                    
        elif args.action == 'test-function':
            # 测试更新特定函数
            if not args.function_name:
                logging.error("使用 test-function 时必须指定 --function-name")
                exit(1)
            
            logging.info(f"测试更新函数 {args.function_name} 的层配置...")
            layer_info = updater.check_layer_status()
            if layer_info:
                try:
                    # 获取函数当前配置
                    current_config = updater.lambda_client.get_function_configuration(
                        FunctionName=args.function_name
                    )
                    logging.info(f"函数当前层配置: {current_config.get('Layers', [])}")
                    
                    # 测试更新
                    updater.update_function_layer(args.function_name, layer_info['arn'])
                    logging.info("函数层配置更新测试完成")
                except Exception as e:
                    logging.error(f"更新函数配置失败: {str(e)}")
                    
    except Exception as e:
        logging.error(f"操作失败: {str(e)}")
        sys.exit(1)
    finally:
        if args.action not in ['cleanup']:  # 避免重复清理
            updater.cleanup_tmp_files(after_update=False)