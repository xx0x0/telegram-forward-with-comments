from utils import setup_logger
from .start_handler import StartHandler
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

class MessageHandler:
    def __init__(self, bot_client, user_client, config, database):
        self.bot_client = bot_client
        self.user_client = user_client
        self.config = config
        self.database = database
        self.logger = setup_logger(__name__)

        self.start_handler = StartHandler(bot_client, config)
        self._register_handlers()
        
        # 添加启动后动作
        self.bot_client.loop.create_task(self._on_bot_start())

    async def _on_bot_start(self):
        """机器人启动后执行的操作"""
        # 设置机器人命令
        await self.set_bot_commands()
        self.logger.info("机器人启动初始化完成")

    def _register_handlers(self):
        """注册命令处理器"""
        # 注册 /start 命令处理器
        @self.bot_client.on(events.NewMessage(pattern='/start'))
        async def start_command(event):
            try:
                await self.start_handler.start_command(event)
            except Exception as e:
                self.logger.error(f"处理 /start 命令时出错: {str(e)}")
                await event.respond("抱歉，处理命令时出现错误，请稍后重试。")
        
        # 注册 /setbotcommands 命令处理器
        @self.bot_client.on(events.NewMessage(pattern='/setbotcommands'))
        async def setbotcommands_command(event):
            try:
                # 检查是否是管理员
                sender = await event.get_sender()
                if str(sender.id) in self.config.get('admin_ids', []):
                    await self.set_bot_commands()
                    await event.respond("机器人命令列表已更新!")
                else:
                    await event.respond("只有管理员才能执行此命令。")
            except Exception as e:
                self.logger.error(f"处理 /setbotcommands 命令时出错: {str(e)}")
                await event.respond("抱歉，设置命令时出现错误，请稍后重试。")
    
    async def set_bot_commands(self):
        """设置机器人命令列表"""
        try:
            commands = [
                BotCommand(command="start", description="开始使用机器人")
            ]
            
            # 创建默认作用域对象（应用于所有用户和聊天）
            scope = BotCommandScopeDefault()
            
            # 支持中文
            language_code = "zh"
            
            # 设置命令
            result = await self.bot_client(
                SetBotCommandsRequest(
                    scope=scope,
                    lang_code=language_code,
                    commands=commands
                )
            )
            if result:
                self.logger.info("成功设置机器人指令列表")
            else:
                self.logger.warning("设置机器人指令列表失败")
                
        except Exception as e:
            self.logger.error(f"设置机器人命令列表时出错: {str(e)}")
            return False