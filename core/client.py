import os
from telethon import TelegramClient as TC
from config import Config
from utils import setup_logger

class TelegramManager:
    def __init__(self, config: Config):
        self.config = config
        self.logger = setup_logger(__name__)

        # 创建 session 文件夹
        session_dir = os.path.join(os.getcwd(), "session")
        os.makedirs(session_dir, exist_ok=True)

        # 设置 bot 和用户的会话文件路径
        user_session_path = os.path.join(session_dir, 'user_session')
        bot_session_path = os.path.join(session_dir, 'bot_session')

        # 用户客户端配置
        user_config = {
            'session': user_session_path,
            "api_id": config.api_id,
            "api_hash": config.api_hash
        }
        
        # 机器人客户端配置
        bot_config = {
            'session': bot_session_path,
            "api_id": config.api_id,
            "api_hash": config.api_hash,
        }

        # 添加代理配置（如果启用）
        if proxy_config := config.proxy_config:
            proxy = {
                'proxy_type': proxy_config['proxy_type'],  # 保持 socks5
                'addr': proxy_config['addr'],
                'port': proxy_config['port'],
                'username': proxy_config['username'] or None,
                'password': proxy_config['password'] or None,
                'rdns': True,
            }
            user_config["proxy"] = proxy
            bot_config["proxy"] = proxy
            self.logger.info(f"Using proxy: {proxy['proxy_type']}://{proxy['addr']}:{proxy['port']}")

        # 初始化两个客户端
        self._user_client = TC(
            **user_config
        )
        self._bot_client = TC(
            **bot_config
        )
        
    async def start(self):
        """启动两个客户端"""
        try:
            self.logger.info("正在启动 user client...")
            await self._user_client.start()
            self.logger.info("User client 启动成功！")
            
            self.logger.info("正在启动 bot client...")
            await self._bot_client.start(bot_token=self.config.bot_token)
            self.logger.info("Bot client 启动成功！")
        except Exception as e:
            self.logger.error(f"连接失败: {str(e)}")
            self.logger.error(f"错误类型: {type(e).__name__}")
            self.logger.exception("详细错误信息:")
            raise
    
    async def stop(self):
        """停止两个客户端"""
        await self._user_client.disconnect()
        await self._bot_client.disconnect()

    @property
    def bot_client(self):
        """获取 bot client"""
        return self._bot_client
    
    @property
    def user_client(self):
        """获取 user client"""
        return self._user_client