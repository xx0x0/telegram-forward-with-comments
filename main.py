import asyncio
from config import Config
from core import (
    TelegramManager, 
    MessageHandler,
    CallbackHandler,
    Database,
    AutoForwardScheduler,
)
from utils import setup_logger
from pathlib import Path

async def main():
    # 创建日志目录
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # 初始化日志
    logger = setup_logger(__name__)
    logger.info("日志初始化完成")

    # 初始化 telegram_manager 和 database 为 None
    telegram_manager = None
    auto_scheduler = None

    try:
        # 初始化配置
        config = Config()
        database = Database()
        
        # 初始化 Telegram 客户端
        logger.info("正在初始化 Telegram 客户端...")
        telegram_manager = TelegramManager(config)
        await telegram_manager.start()
        logger.info("Telegram 客户端初始化完成")
        
        # 获取客户端实例
        bot_client = telegram_manager.bot_client
        user_client = telegram_manager.user_client
        
        # 初始化处理程序
        logger.info("开始初始化处理程序...")
        message_handler = MessageHandler(bot_client, user_client, config, database)
        callback_handler = CallbackHandler(bot_client, user_client, database)
        # ForwardHandler 现在由 MessageHandler 内部初始化
        logger.info("处理程序初始化完成")
        
        # 初始化并启动自动转发调度器
        logger.info("正在启动自动转发调度器...")
        auto_scheduler = AutoForwardScheduler(user_client, database)
        await auto_scheduler.start()
        logger.info("自动转发调度器启动完成")
        
        # 创建一个永不完成的任务来保持程序运行
        try:
            logger.info("机器人已启动，等待消息...")
            await asyncio.gather(
                bot_client.run_until_disconnected(),
            )
        except KeyboardInterrupt:
            logger.info("接收到键盘中断信号")
            pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("正在关闭程序...")
    except Exception as e:
        logger.error(f"程序运行错误: {str(e)}", exc_info=True)
    finally:
        # 关闭自动转发调度器
        if auto_scheduler:
            logger.info("正在停止自动转发调度器...")
            await auto_scheduler.stop()
            
        # 关闭Telegram客户端
        if telegram_manager:
            logger.info("正在关闭Telegram客户端...")
            await telegram_manager.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
