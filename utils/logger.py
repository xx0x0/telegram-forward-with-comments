from loguru import logger
import os
import sys
import yaml
 
def setup_logger(name: str, level: str = 'INFO') -> None:
    
    # 读取配置文件，获取日志配置
    with open("config.yml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    # 获取日志配置
    log_file = config["logging"]["log_path"]
    log_dir = os.path.dirname(log_file)

    # 确保日志目录存在
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 移除默认的handler
    logger.remove()

    # 添加控制台输出
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level.upper(),
        enqueue=True,
        colorize=True
    )

    # 添加文件输出
    logger.add(
        log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=level.upper(),
        rotation="10 MB",  # 日志文件达到10MB时轮转
        compression="zip",  # 压缩旧的日志文件
        enqueue=True
    )
    
     # 创建一个带有上下文信息的logger
    return logger.bind(name=name)