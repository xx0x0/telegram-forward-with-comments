from utils import setup_logger
from telethon import events, Button
from .target_channel_callback import TargetChannelHandler
from .source_channel_callback import SourceChannelHandler
import re # 确保 re 导入在文件顶部

class CallbackHandler:
    def __init__(self, bot_client, user_client, database):
        self.bot_client = bot_client
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        
        # 初始化子处理器
        self.target_handler = TargetChannelHandler(bot_client, user_client, database)
        self.source_handler = SourceChannelHandler(bot_client, user_client, database)
        
        self.register_callbacks()
        
    def register_callbacks(self):
        """注册所有回调处理函数"""
        
        # --- 基础回调 ---
        @self.bot_client.on(events.CallbackQuery(pattern=b'target_channel'))
        async def handle_target_channel_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            await self.target_handler.show_target_channels(event)

        @self.bot_client.on(events.CallbackQuery(pattern=b'source_channel'))
        async def handle_source_channel_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            await self.source_handler.show_source_channels(event)
        
        @self.bot_client.on(events.CallbackQuery(pattern=b'add_target'))
        async def handle_add_target_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            await self.target_handler.handle_add_target(event)

        @self.bot_client.on(events.CallbackQuery(pattern=b'add_source'))
        async def handle_add_source_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            await self.source_handler.handle_add_source(event)

        # 请注意，delete_target 和 delete_source 通常会直接跳转到详情页的确认删除，
        # 所以这里的 pattern 应该与您实际的按钮data匹配
        # 如果您的delete_target按钮的data是 delete_target_123，那下面就不能用 b'delete_target'
        # 而是用 r'delete_target_(\d+)' 来匹配
        
        # 确保这些回调与您的按钮 data 匹配
        @self.bot_client.on(events.CallbackQuery(pattern=r'delete_target_(\d+)'))
        async def handle_delete_target_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            target_id = event.pattern_match.group(1).decode('utf-8')
            await self.target_handler.delete_target_channel(event, target_id)

        @self.bot_client.on(events.CallbackQuery(pattern=r'delete_source_detail_(\d+)'))
        async def handle_delete_source_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.delete_source_channel_from_detail(event, source_id)

        @self.bot_client.on(events.CallbackQuery(pattern=b'back_to_home'))
        async def handle_back_to_home_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            await self._back_to_home(event)

        # --- 目标频道详情相关回调处理 ---
        # 处理特定目标频道（例如 target_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'target_(\d+)'))
        async def handle_target_detail_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            target_id = event.pattern_match.group(1).decode('utf-8')
            await self.target_handler.show_target_channel_detail(event, target_id)
            
        # 处理确认删除目标频道（例如 confirm_delete_target_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'confirm_delete_target_(\d+)'))
        async def handle_confirm_delete_target_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            target_id = event.pattern_match.group(1).decode('utf-8')
            await self.target_handler.confirm_delete_target(event, target_id)

        # --- 资源频道详情相关回调处理 ---
        # 显示资源频道详情（例如 source_detail_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'source_detail_(\d+)'))
        async def handle_source_detail_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.show_source_channel_detail(event, source_id)
        
        # 切换转发状态（例如 toggle_transfer_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'toggle_transfer_(\d+)'))
        async def handle_toggle_transfer_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.toggle_transfer_status(event, source_id)
        
        # 切换发送类型（例如 toggle_send_type_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'toggle_send_type_(\d+)'))
        async def handle_toggle_send_type_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.toggle_send_type(event, source_id)
        
        # 设置发送频率（例如 set_interval_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'set_interval_(\d+)'))
        async def handle_set_interval_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.handle_set_interval(event, source_id)
        
        # 选择目标频道（例如 select_target_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'select_target_(\d+)'))
        async def handle_select_target_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.handle_select_target(event, source_id)
        
        # 设置目标频道（例如 set_target_源ID_目标ID）
        @self.bot_client.on(events.CallbackQuery(pattern=r'set_target_(\d+)_(-?\d+)'))
        async def handle_set_target_channel_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            target_channel_id = event.pattern_match.group(2).decode('utf-8')
            await self.source_handler.set_target_channel(event, source_id, target_channel_id)
        
        # 确认删除资源频道（例如 confirm_delete_source_detail_123）
        @self.bot_client.on(events.CallbackQuery(pattern=r'confirm_delete_source_detail_(\d+)'))
        async def handle_confirm_delete_source_detail_button(event):
            self.logger.info(f"收到回调: {event.data.decode('utf-8')}")
            source_id = event.pattern_match.group(1).decode('utf-8')
            await self.source_handler.confirm_delete_source_from_detail(event, source_id)


        # --- 消息输入处理 ---
        # 这是一个 NewMessage 事件，通常应该放在 main.py 或者专门的 message_handlers.py 中
        # 如果您确定要放在这里，请确保在 main.py 中没有重复注册 NewMessage 事件
        @self.bot_client.on(events.NewMessage(func=lambda e: e.is_private))
        async def input_handler(event):
            try:
                # 尝试处理作为目标频道输入
                if await self.target_handler.process_channel_input(event):
                    return
                
                # 尝试处理作为资源频道输入
                if await self.source_handler.process_channel_input(event):
                    return
                
            except Exception as e:
                self.logger.error(f"处理输入时出错: {str(e)}")
                await event.respond(f"处理输入出错: {str(e)}")

    async def _back_to_home(self, event):
        """返回首页"""
        # 获取机器人用户名
        # bot_me = await self.bot_client.get_me() # 这一行其实在这里用不到，可以删除，或者如果其他地方用到了就保留
        
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
        
        await event.edit(start_text, buttons=buttons)