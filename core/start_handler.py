from telethon.events import NewMessage
from telethon import Button
from utils import setup_logger

class StartHandler:
    def __init__(self, bot_client, config):
        self.bot_client = bot_client
        self.config = config
        self.logger = setup_logger(__name__)
        

    async def start_command(self, event: NewMessage.Event):
        """
        处理 /start 命令
        """

        # 获取机器人用户名
        bot_me = await self.bot_client.get_me()
        bot_username = bot_me.username
        
        try:
            start_text = (
                f"🤖 全自动下载转发机器人\n\n"
                "📝 使用教程：\n"
                "1️⃣ 先添加目标频道（接收内容的频道）\n"
                "2️⃣ 再添加资源频道（提供内容的频道）\n"
                "3️⃣ 完成配置后即可自动转发\n\n"
                "🔹 支持私密群组和频道\n"
                "🔹 支持转发限制频道（自动下载再上传）\n"
                "🔹 支持添加多个转发规则\n"
                "🔹 支持自定义转发文本\n\n"
                "点击下方按钮开始配置："
            )
            
            # 创建两个按钮
            buttons = [
                [
                    Button.inline("🎯 目标频道", data="target_channel"),
                    Button.inline("📡 资源频道", data="source_channel")
                ]
            ]
            
            await event.respond(start_text, buttons=buttons)
            
        except Exception as e:
            self.logger.error(f"处理 start 命令出错: {str(e)}")