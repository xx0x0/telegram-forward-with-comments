from telethon import events, Button
from utils import setup_logger
import re

class SourceChannelHandler:
    def __init__(self, bot_client, user_client, database):
        self.bot_client = bot_client
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        # 用于存储用户当前操作状态
        self.user_states = {}

    async def show_source_channels(self, event):
        """显示所有资源频道"""
        try:
            # 查询数据库获取所有资源频道及其对应的目标频道名称
            result = self.database.execute(
                """
                SELECT rc.id, rc.channel_id, rc.channel_name, tc.channel_name as target_channel_name
                FROM resource_channels rc
                LEFT JOIN target_channels tc ON rc.target_channel_id = tc.channel_id
                ORDER BY rc.id DESC
                """
            )
            
            text = ""
            buttons = []

            if not result:
                # 如果没有频道，显示空列表和添加按钮
                text = "📋 资源频道列表为空\n\n点击下方按钮添加频道"
                buttons = [
                    [Button.inline("➕ 添加频道", data="add_source")],
                    [Button.inline("🔙 返回首页", data="back_to_home")]
                ]
            else:
                # 构建频道列表，每行两个按钮
                text = "📋 资源频道列表：\n\n选择频道进行操作"
                row = []
                
                for i, (db_id, channel_id, channel_name, target_channel_name) in enumerate(result):
                    display_name = f"{channel_name} {'➡️ ' + target_channel_name if target_channel_name else ''}"
                    btn = Button.inline(
                        display_name, 
                        data=f"source_detail_{db_id}"
                    )
                    row.append(btn)
                    
                    # 每两个按钮一行，或者如果是最后一个按钮
                    if len(row) == 2 or i == len(result) - 1:
                        buttons.append(row)
                        row = []
                
                # 添加底部操作按钮
                buttons.append([
                    Button.inline("➕ 添加", data="add_source"),
                    Button.inline("🔙 返回首页", data="back_to_home")
                ])
            
            # 统一在这里发送或更新消息
            if hasattr(event, 'edit') and not isinstance(event, events.NewMessage.Event):
                await event.edit(text, buttons=buttons)
            else:
                await event.respond(text, buttons=buttons)
                
        except Exception as e:
            self.logger.error(f"显示资源频道列表时出错: {str(e)}")
            await event.answer(f"显示资源频道列表出错: {str(e)}")

    async def handle_add_source(self, event):
        """处理添加资源频道的请求"""
        user_id = event.chat_id
        self.user_states[user_id] = {'action': 'waiting_for_source_channel'}
        text = "请发送资源频道（提供转发内容的频道）的链接或ID。\n\n" \
               "确保用户账号已加入该频道。"
        buttons = [[Button.inline("🔙 取消", data="source_channel")]]
        await event.edit(text, buttons=buttons)

    async def process_channel_input(self, event):
        """处理用户发送的资源频道链接或ID"""
        user_id = event.chat_id
        if user_id not in self.user_states or self.user_states[user_id]['action'] != 'waiting_for_source_channel':
            return False # 不处理非资源频道输入的普通消息

        channel_identifier = event.text.strip()
        try:
            entity = await self.user_client.get_entity(channel_identifier)
            channel_id = str(entity.id)
            channel_name = entity.title

            # 检查频道是否已存在
            existing_channel = self.database.execute(
                "SELECT id FROM resource_channels WHERE channel_id = ?", 
                (channel_id,)
            )
            if existing_channel:
                await event.respond("该资源频道已存在！", buttons=[[Button.inline("🔙 返回", data="source_channel")]])
                del self.user_states[user_id]
                return True

            self.database.execute(
                "INSERT INTO resource_channels (channel_id, channel_name, is_transfer, send_type, send_interval) VALUES (?, ?, ?, ?, ?)",
                (channel_id, channel_name, 0, 0, 0) # 默认不转发，正常转发，无间隔
            )
            
            # 清除用户状态
            del self.user_states[user_id]
            
            # 提示添加成功，并自动返回频道列表
            await event.respond(f"成功添加资源频道：\n{channel_name}\n频道ID: {channel_id}")
            await self.show_source_channels(event) # 显示更新后的列表
            return True
            
        except ValueError:
            await event.respond(
                "无效的频道链接或ID，请重新发送。\n\n"
                "例如：`https://t.me/telegram` 或 `@telegram` 或 `123456789`",
                buttons=[[Button.inline("🔙 返回", data="source_channel")]]
            )
            return True
        except Exception as e:
            self.logger.error(f"处理频道链接出错: {str(e)}")
            error_msg = f"添加频道出错: {str(e)}"
            await event.respond(
                error_msg, 
                buttons=[[Button.inline("🔙 返回", data="source_channel")]]
            )
            return True
    
    async def show_source_channel_detail(self, event, source_id):
        """显示资源频道详细配置"""
        try:
            # 查询数据库获取资源频道详情
            result = self.database.execute(
                """
                SELECT rc.id, rc.channel_id, rc.channel_name, rc.target_channel_id, 
                        rc.is_transfer, rc.send_type, rc.send_interval,
                        tc.channel_name as target_channel_name
                FROM resource_channels rc
                LEFT JOIN target_channels tc ON rc.target_channel_id = tc.channel_id
                WHERE rc.id = ?
                """, 
                (source_id,)
            )
            
            if not result:
                await event.edit(
                    "未找到该频道", 
                    buttons=[[Button.inline("🔙 返回", data="source_channel")]]
                )
                return
            
            # 解析频道信息
            (db_id, channel_id, channel_name, target_channel_id, 
             is_transfer, send_type, send_interval, target_channel_name) = result[0]
            
            # 构建显示文本
            text = f"📺 {channel_name}\n\n"
            
            # 显示目标频道
            if target_channel_name:
                text += f"1. 目标频道：{target_channel_name}\n"
            else:
                text += "1. 目标频道：未设置\n"
            
            # 显示转发状态
            transfer_status = "关闭" if is_transfer == 0 else "开启"
            text += f"2. 转发状态：{transfer_status}\n"
            
            # 显示转发方式
            send_type_text = "正常转发" if send_type == 0 else "评论转发"
            text += f"3. 转发方式：{send_type_text}\n"
            
            # 显示转发频率
            interval_text = f"{send_interval}小时每次" if send_interval else "未设置"
            text += f"4. 转发频率：{interval_text}\n"
            
            # 构建URL链接
            if str(channel_id).startswith("-100"):
                # 私有频道
                chat_id = str(channel_id)[4:]  # 移除-100前缀
                channel_link = f"https://t.me/c/{chat_id}"
            else:
                # 试图获取公开频道用户名
                try:
                    entity = await self.user_client.get_entity(int(channel_id))
                    if hasattr(entity, 'username') and entity.username:
                        channel_link = f"https://t.me/{entity.username}"
                    else:
                        channel_link = f"https://t.me/c/{channel_id}"
                except Exception as link_e:
                    self.logger.warning(f"无法为资源频道 {channel_name} ({channel_id}) 获取链接: {link_e}")
                    channel_link = f"无法获取链接 (ID: {channel_id})"
            
            # 构建按钮
            buttons = [
                [Button.url("查看频道内容", channel_link)],
                [
                    Button.inline(
                        "开启转发" if is_transfer == 0 else "关闭转发", 
                        data=f"toggle_transfer_{source_id}"
                    ),
                    Button.inline(
                        "评论转发" if send_type == 0 else "正常转发", 
                        data=f"toggle_send_type_{source_id}"
                    )
                ],
                [
                    Button.inline("设置发送频率", data=f"set_interval_{source_id}"),
                    Button.inline("配置目标频道", data=f"select_target_{source_id}")
                ],
                [
                    Button.inline("❌ 删除", data=f"delete_source_detail_{source_id}"),
                    Button.inline("🔙 返回", data="source_channel")
                ]
            ]
            
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            self.logger.error(f"显示资源频道详情出错: {str(e)}")
            await event.respond(f"显示频道详情时出错: {str(e)}")
    
    async def toggle_transfer_status(self, event, source_id):
        """切换转发状态"""
        try:
            # 查询当前状态
            result = self.database.execute(
                "SELECT is_transfer FROM resource_channels WHERE id = ?", 
                (source_id,)
            )
            
            if not result:
                await event.answer("频道不存在")
                return
            
            current_status = result[0][0]
            new_status = 1 if current_status == 0 else 0
            
            # 更新状态
            self.database.execute(
                "UPDATE resource_channels SET is_transfer = ? WHERE id = ?", 
                (new_status, source_id)
            )
            
            # 显示更新后的状态
            status_text = "开启" if new_status == 1 else "关闭"
            await event.answer(f"已{status_text}转发")
            
            # 刷新详情页，编辑原消息而不是发送新消息
            await self.show_source_channel_detail(event, source_id)
            
        except Exception as e:
            self.logger.error(f"切换转发状态出错: {str(e)}")
            await event.answer(f"操作失败: {str(e)}")
    
    async def toggle_send_type(self, event, source_id):
        """切换发送类型"""
        try:
            # 查询当前状态
            result = self.database.execute(
                "SELECT send_type FROM resource_channels WHERE id = ?", 
                (source_id,)
            )
            
            if not result:
                await event.answer("频道不存在")
                return
            
            current_type = result[0][0]
            new_type = 1 if current_type == 0 else 0
            
            # 更新状态
            self.database.execute(
                "UPDATE resource_channels SET send_type = ? WHERE id = ?", 
                (new_type, source_id)
            )
            
            # 显示更新后的状态
            type_text = "正常转发" if new_type == 0 else "评论转发"
            await event.answer(f"已设置为{type_text}")
            
            # 刷新详情页，编辑原消息而不是发送新消息
            await self.show_source_channel_detail(event, source_id)
            
        except Exception as e:
            self.logger.error(f"切换发送类型出错: {str(e)}")
            await event.answer(f"操作失败: {str(e)}")
    
    async def handle_set_interval(self, event, source_id):
        """处理设置发送频率"""
        user_id = event.chat_id
        
        # 设置用户状态为等待输入频率
        self.user_states[user_id] = {
            'action': 'setting_interval',
            'source_id': source_id
        }
        
        text = (
            "请输入发送频率（单位：小时）\n\n"
            "例如：输入 1 表示每1小时转发一次\n"
            "输入 0 表示立即转发（不等待）"
        )
        
        buttons = [[Button.inline("🔙 取消", data=f"source_detail_{source_id}")]]
        
        await event.edit(text, buttons=buttons)
    
    async def process_interval_input(self, event):
        """处理用户输入的发送频率"""
        user_id = event.chat_id
        if user_id not in self.user_states or self.user_states[user_id]['action'] != 'setting_interval':
            return False

        source_id = self.user_states[user_id]['source_id']
        try:
            interval = int(event.text.strip())
            if interval < 0:
                raise ValueError("频率不能为负数")

            self.database.execute(
                "UPDATE resource_channels SET send_interval = ? WHERE id = ?",
                (interval, source_id)
            )
            del self.user_states[user_id]
            await event.respond(f"发送频率已设置为 {interval} 小时。", buttons=[[Button.inline("🔙 返回", data=f"source_detail_{source_id}")]])
            await self.show_source_channel_detail(event, source_id)
            return True
        except ValueError:
            await event.respond("请输入一个有效的整数（例如：1 或 0）。", buttons=[[Button.inline("🔙 取消", data=f"source_detail_{source_id}")]])
            return True
        except Exception as e:
            self.logger.error(f"设置发送频率时出错: {str(e)}")
            await event.respond(f"设置失败: {str(e)}", buttons=[[Button.inline("🔙 取消", data=f"source_detail_{source_id}")]])
            return True

    async def handle_select_target(self, event, source_id):
        """处理选择目标频道"""
        try:
            # 查询所有可用的目标频道
            target_channels = self.database.execute(
                "SELECT id, channel_id, channel_name FROM target_channels ORDER BY id DESC"
            )
            
            if not target_channels:
                await event.answer("没有可用的目标频道，请先添加目标频道。")
                # 返回资源频道详情页
                await self.show_source_channel_detail(event, source_id) 
                return
            
            text = "请选择目标频道："
            buttons = []
            
            # 添加所有目标频道按钮
            for tc_id, tc_channel_id, tc_name in target_channels:
                buttons.append([Button.inline(tc_name, data=f"set_target_{source_id}_{tc_channel_id}")])
            
            buttons.append([Button.inline("🔙 返回", data=f"source_detail_{source_id}")])
            
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            self.logger.error(f"选择目标频道时出错: {str(e)}")
            await event.answer(f"操作失败: {str(e)}")

    async def set_target_channel(self, event, source_id, target_channel_id):
        """设置资源频道的目标频道"""
        try:
            # 获取目标频道名称用于提示
            target_info = self.database.execute(
                "SELECT channel_name FROM target_channels WHERE channel_id = ?", (target_channel_id,)
            )
            target_name = target_info[0][0] if target_info else "未知频道"

            self.database.execute(
                "UPDATE resource_channels SET target_channel_id = ? WHERE id = ?",
                (target_channel_id, source_id)
            )
            await event.answer(f"已将此资源频道的目标频道设置为：{target_name}")
            await self.show_source_channel_detail(event, source_id) # 刷新详情页
        except Exception as e:
            self.logger.error(f"设置目标频道时出错: {str(e)}")
            await event.answer(f"设置失败: {str(e)}")

    async def delete_source_channel_from_detail(self, event, source_id):
        """从详情页处理删除资源频道"""
        source_channel = self.database.execute(
            "SELECT channel_name FROM resource_channels WHERE id = ?", (source_id,)
        )
        if not source_channel:
            await event.answer("频道不存在")
            await self.show_source_channels(event)
            return

        channel_name = source_channel[0][0]
        text = f"确定要删除资源频道：{channel_name} 吗？"
        buttons = [
            [Button.inline("✅ 确认删除", data=f"confirm_delete_source_detail_{source_id}")],
            [Button.inline("🔙 取消", data=f"source_detail_{source_id}")] # 返回详情页
        ]
        await event.edit(text, buttons=buttons)

    async def confirm_delete_source_from_detail(self, event, source_id):
        """确认删除资源频道（从详情页进入）"""
        try:
            # 获取频道名称用于提示
            channel_info = self.database.execute(
                "SELECT channel_name FROM resource_channels WHERE id = ?", (source_id,)
            )
            if not channel_info:
                await event.answer("频道已不存在。")
                await self.show_source_channels(event)
                return

            channel_name = channel_info[0][0]

            self.database.execute(
                "DELETE FROM resource_channels WHERE id = ?", (source_id,)
            )
            self.logger.info(f"已删除资源频道 ID: {source_id}, 名称: {channel_name}")
            await event.answer(f"资源频道 {channel_name} 已删除。")
            await self.show_source_channels(event) # 刷新列表
        except Exception as e:
            self.logger.error(f"删除资源频道时出错: {str(e)}")
            await event.answer(f"删除失败: {str(e)}")

    async def handle_delete_source(self, event):
        """处理删除资源频道（可能从列表页进入）"""
        # 这个函数可能需要一个更通用的处理逻辑，或者直接跳转到show_source_channels，
        # 并依赖用户点击"❌ 删除"进入确认流程
        await self.show_source_channels(event) # 暂时直接显示列表，让用户从详情页删除
        await event.answer("请从频道详情页删除资源频道。") # 给个提示