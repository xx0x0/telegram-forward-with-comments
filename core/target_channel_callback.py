from telethon import events, Button
from utils import setup_logger

class TargetChannelHandler:
    def __init__(self, bot_client, user_client, database):
        self.bot_client = bot_client
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        # 用于存储用户当前操作状态
        self.user_states = {}

    async def show_target_channels(self, event):
        """显示所有目标频道"""
        try:
            # 查询数据库获取所有目标频道
            result = self.database.execute(
                "SELECT id, channel_id, channel_name FROM target_channels ORDER BY id DESC"
            )
            
            text = ""
            buttons = []

            if not result:
                # 如果没有频道，显示空列表和添加按钮
                text = "📋 目标频道列表为空\n\n点击下方按钮添加频道"
                buttons = [
                    [Button.inline("➕ 添加频道", data="add_target")],
                    [Button.inline("🔙 返回首页", data="back_to_home")]
                ]
            else:
                # 构建频道列表，每行两个按钮
                text = "📋 目标频道列表：\n\n选择频道进行操作"
                row = []
                
                for i, (db_id, channel_id, channel_name) in enumerate(result):
                    # 构建按钮，使用db_id作为标识
                    btn = Button.inline(
                        f"{channel_name}", 
                        data=f"target_{db_id}"
                    )
                    row.append(btn)
                    
                    # 每两个按钮一行，或者如果是最后一个按钮
                    if len(row) == 2 or i == len(result) - 1:
                        buttons.append(row)
                        row = []
                
                # 添加底部操作按钮
                buttons.append([
                    Button.inline("➕ 添加", data="add_target"),
                    Button.inline("🔙 返回首页", data="back_to_home")
                ])
            
            # 统一在这里发送或更新消息
            if hasattr(event, 'edit') and not isinstance(event, events.NewMessage.Event):
                await event.edit(text, buttons=buttons)
            else:
                await event.respond(text, buttons=buttons)
                
        except Exception as e:
            self.logger.error(f"显示目标频道列表时出错: {str(e)}")
            # 弹出一个临时提示，而不是修改消息内容
            await event.answer(f"显示频道列表出错: {str(e)}") 

    async def handle_add_target(self, event):
        """处理添加目标频道的请求"""
        user_id = event.chat_id
        self.user_states[user_id] = {'action': 'waiting_for_target_channel'}
        text = "请发送目标频道（接收转发内容的频道）的链接或ID。\n\n" \
               "确保机器人账号已加入该频道并拥有发送消息的权限。"
        buttons = [[Button.inline("🔙 取消", data="target_channel")]]
        await event.edit(text, buttons=buttons)

    async def process_channel_input(self, event):
        """处理用户发送的频道链接或ID"""
        user_id = event.chat_id
        if user_id not in self.user_states or self.user_states[user_id]['action'] != 'waiting_for_target_channel':
            return False # 不处理非目标频道输入的普通消息

        channel_identifier = event.text.strip()
        try:
            entity = await self.user_client.get_entity(channel_identifier)
            channel_id = str(entity.id)
            channel_name = entity.title

            # 检查频道是否已存在
            existing_channel = self.database.execute(
                "SELECT id FROM target_channels WHERE channel_id = ?", 
                (channel_id,)
            )
            if existing_channel:
                await event.respond("该目标频道已存在！", buttons=[[Button.inline("🔙 返回", data="target_channel")]])
                del self.user_states[user_id]
                return True

            self.database.execute(
                "INSERT INTO target_channels (channel_id, channel_name) VALUES (?, ?)",
                (channel_id, channel_name)
            )
            
            # 清除用户状态
            del self.user_states[user_id]
            
            # 提示添加成功，并自动返回频道列表
            await event.respond(f"成功添加目标频道：\n{channel_name}\n频道ID: {channel_id}")
            await self.show_target_channels(event) # 显示更新后的列表
            return True
            
        except ValueError:
            await event.respond(
                "无效的频道链接或ID，请重新发送。\n\n"
                "例如：`https://t.me/telegram` 或 `@telegram` 或 `123456789`",
                buttons=[[Button.inline("🔙 返回", data="target_channel")]]
            )
            return True
        except Exception as e:
            self.logger.error(f"处理频道链接出错: {str(e)}")
            error_msg = f"添加频道出错: {str(e)}"
            await event.respond(
                error_msg, 
                buttons=[[Button.inline("🔙 返回", data="target_channel")]]
            )
            return True

    async def show_target_channel_detail(self, event, target_id):
        """显示目标频道详情和操作选项"""
        try:
            result = self.database.execute(
                "SELECT id, channel_id, channel_name FROM target_channels WHERE id = ?",
                (target_id,)
            )
            if not result:
                await event.edit(
                    "未找到该频道。",
                    buttons=[[Button.inline("🔙 返回", data="target_channel")]]
                )
                return

            db_id, channel_id, channel_name = result[0]

            # 尝试构建频道链接
            channel_link = ""
            try:
                if str(channel_id).startswith("-100"):
                    # 私有频道，通过 chat_id 构造链接
                    chat_id = str(channel_id)[4:]
                    channel_link = f"https://t.me/c/{chat_id}"
                else:
                    # 尝试获取公开频道用户名
                    entity = await self.user_client.get_entity(int(channel_id))
                    if hasattr(entity, 'username') and entity.username:
                        channel_link = f"https://t.me/{entity.username}"
                    else:
                        channel_link = f"https://t.me/c/{channel_id}"
            except Exception as link_e:
                self.logger.warning(f"无法为目标频道 {channel_name} ({channel_id}) 获取链接: {link_e}")
                channel_link = f"无法获取链接 (ID: {channel_id})"

            text = f"🎯 目标频道详情：\n\n" \
                   f"名称：{channel_name}\n" \
                   f"ID：{channel_id}\n\n" \
                   f"链接：{channel_link}"

            buttons = [
                [Button.inline("❌ 删除", data=f"delete_target_{db_id}")],
                [Button.inline("🔙 返回", data="target_channel")]
            ]
            await event.edit(text, buttons=buttons)

        except Exception as e:
            self.logger.error(f"显示目标频道详情时出错: {str(e)}")
            await event.answer(f"显示详情出错: {str(e)}")

    async def delete_target_channel(self, event, target_id):
        """删除目标频道前确认"""
        target_channel = self.database.execute(
            "SELECT channel_name FROM target_channels WHERE id = ?", (target_id,)
        )
        if not target_channel:
            await event.answer("频道不存在")
            await self.show_target_channels(event)
            return

        channel_name = target_channel[0][0]
        text = f"确定要删除目标频道：{channel_name} 吗？\n\n这将不会影响已经转发的内容。"
        buttons = [
            [Button.inline("✅ 确认删除", data=f"confirm_delete_target_{target_id}")],
            [Button.inline("🔙 取消", data=f"target_{target_id}")] # 返回详情页
        ]
        await event.edit(text, buttons=buttons)

    async def confirm_delete_target(self, event, target_id):
        """确认删除目标频道"""
        try:
            # 获取频道名称用于提示
            channel_info = self.database.execute(
                "SELECT channel_name FROM target_channels WHERE id = ?", (target_id,)
            )
            if not channel_info:
                await event.answer("频道已不存在。")
                await self.show_target_channels(event)
                return

            channel_name = channel_info[0][0]

            self.database.execute(
                "DELETE FROM target_channels WHERE id = ?", (target_id,)
            )
            self.logger.info(f"已删除目标频道 ID: {target_id}, 名称: {channel_name}")
            await event.answer(f"目标频道 {channel_name} 已删除。")
            await self.show_target_channels(event) # 刷新列表
        except Exception as e:
            self.logger.error(f"删除目标频道时出错: {str(e)}")
            await event.answer(f"删除失败: {str(e)}")