from telethon import events, utils
from utils import setup_logger
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
from core.restricted_normal_forward_handler import RestrictedNormalForwardHandler
import asyncio
import traceback
import re

class NormalForwardHandler:
    def __init__(self, user_client, database):
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        self.restricted_handler = RestrictedNormalForwardHandler(user_client, database)
        
        # 广告关键词列表
        self.ad_keywords = [
            "推广", "广告", "促销", "优惠", "折扣", "抢购", "限时", "活动",
            "官网", "链接", "联系", "咨询", "电话", "微信", "加群", "加入",
            "ad", "sponsor", "promotion", "discount", "offer", "sale",
            "buy", "price", "contact", "join", "group", "channel", "vip"
        ]
        
        # URL检测正则表达式（匹配非超链接形式的URL）
        self.url_pattern = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)
    
    async def forward_messages(self, source_channel_id):
        """从源频道转发消息到目标频道"""
        try:
            # 获取源频道和目标频道信息
            source_info = self.database.execute(
                """SELECT channel_id, channel_name, target_channel_id 
                   FROM resource_channels WHERE channel_id = ? AND is_transfer = 1""",
                (source_channel_id,)
            )
            
            if not source_info:
                self.logger.error(f"未找到频道ID为 {source_channel_id} 的可转发频道")
                return False
            
            source_channel_id, source_name, target_channel_id = source_info[0]
            
            if not target_channel_id:
                self.logger.error(f"频道 {source_name} 未设置目标频道")
                return False
            
            # 获取已经转发过的消息ID
            forwarded_messages = self.database.execute(
                "SELECT message_id FROM resource_messages WHERE channel_id = ?", 
                (source_channel_id,)
            )
            
            forwarded_ids = set(msg[0] for msg in forwarded_messages) if forwarded_messages else set()
            self.logger.info(f"已有 {len(forwarded_ids)} 条消息被标记为已处理")
            
            # 初始化变量
            last_id = 0  # 用于记录获取消息的最小ID
            batch_size = 30  # 每批获取的消息数量
            found_valid_message = False  # 是否找到符合条件的消息
            
            # 循环获取消息，直到找到符合条件的消息或到达频道底部
            while True:
                # 构建获取消息的参数
                get_msg_params = {
                    "entity": int(source_channel_id),
                    "limit": batch_size
                }
                
                if last_id > 0:
                    get_msg_params["max_id"] = last_id  # 获取ID小于last_id的消息
                
                # 获取一批消息
                messages = await self.user_client.get_messages(**get_msg_params)
                
                if not messages:
                    self.logger.info("没有更多消息可获取，已到达频道底部")
                    break
                
                self.logger.info(f"获取到 {len(messages)} 条消息")
                
                # 更新最后处理的消息ID
                message_ids = [msg.id for msg in messages]
                last_id = min(message_ids)
                
                # 过滤出未转发过的消息
                new_messages = [msg for msg in messages if str(msg.id) not in forwarded_ids]
                
                if not new_messages:
                    self.logger.info("本批次所有消息已处理，继续获取更早的消息")
                    # 添加3秒延迟，避免频繁请求
                    await asyncio.sleep(3)
                    continue
                
                self.logger.info(f"本批次有 {len(new_messages)} 条未处理的消息")
                
                # 过滤出符合条件的消息
                filtered_messages = []
                for msg in new_messages:
                    if self._should_forward_message(msg):
                        filtered_messages.append(msg)
                        found_valid_message = True
                    else:
                        self.logger.info(f"消息 {msg.id} 不符合转发条件，标记为已处理")
                        self.database.execute(
                            "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
                            (source_channel_id, str(msg.id))
                        )
                        forwarded_ids.add(str(msg.id))
                
                if not filtered_messages:
                    self.logger.info("本批次没有符合条件的消息，继续获取更早的消息")
                    # 添加3秒延迟，避免频繁请求
                    await asyncio.sleep(3)
                    continue
                
                self.logger.info(f"找到 {len(filtered_messages)} 条符合转发条件的消息")
                
                # 处理最新的一条符合条件的消息
                latest_message = filtered_messages[0]  # 取最新的一条
                
                # 检查是否为媒体组消息
                if latest_message.grouped_id:
                    self.logger.info(f"检测到媒体组消息，组ID: {latest_message.grouped_id}")
                    
                    # 收集同一组ID的所有消息
                    grouped_messages = []
                    current_group_id = latest_message.grouped_id
                    
                    # 从过滤后的消息中先收集媒体组消息
                    initial_group_msgs = []
                    for msg in filtered_messages:
                        if msg.grouped_id == current_group_id:
                            initial_group_msgs.append(msg)
                    
                    # 获取已知组内消息的ID范围
                    msg_ids = [msg.id for msg in initial_group_msgs]
                    if msg_ids:
                        min_id, max_id = min(msg_ids), max(msg_ids)
                        self.logger.info(f"初步收集媒体组消息，ID范围: {min_id} - {max_id}，共 {len(initial_group_msgs)} 条")
                        
                        # 获取潜在的完整媒体组
                        # 由于可能出现ID不连续的情况，先尝试向下扩大范围
                        try:
                            # 向下获取可能的组消息 (id < min_id)
                            lower_msgs = await self.user_client.get_messages(
                                entity=int(source_channel_id),
                                max_id=min_id-1,  # 小于最小ID
                                limit=10  # 限制获取数量避免过多
                            )
                            
                            # 向上获取可能的组消息 (id > max_id)
                            higher_msgs = await self.user_client.get_messages(
                                entity=int(source_channel_id),
                                min_id=max_id,  # 大于最大ID
                                limit=10  # 限制获取数量避免过多
                            )
                            
                            # 合并可能的组消息
                            potential_group_msgs = lower_msgs + higher_msgs
                            
                            # 过滤出同组ID的消息
                            for msg in potential_group_msgs:
                                if msg.grouped_id == current_group_id and msg.id not in msg_ids:
                                    if self._should_forward_message(msg):
                                        initial_group_msgs.append(msg)
                                        msg_ids.append(msg.id)
                                    else:
                                        # 标记不符合条件的消息为已处理
                                        self.database.execute(
                                            "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
                                            (source_channel_id, str(msg.id))
                                        )
                                        forwarded_ids.add(str(msg.id))
                            
                            # 更新ID范围
                            min_id, max_id = min(msg_ids), max(msg_ids)
                            self.logger.info(f"扩展搜索后收集到媒体组消息，ID范围: {min_id} - {max_id}，共 {len(initial_group_msgs)} 条")
                            
                            # 检查是否需要获取更精确的范围内消息
                            expected_count = max_id - min_id + 1
                            if len(initial_group_msgs) < expected_count:
                                self.logger.info(f"媒体组可能不完整，需要获取更精确的范围内消息，预期 {expected_count} 条")
                                
                                # 获取精确ID范围内的所有消息
                                precise_msgs = await self.user_client.get_messages(
                                    entity=int(source_channel_id),
                                    min_id=min_id-1,
                                    max_id=max_id+1
                                )
                                
                                for msg in precise_msgs:
                                    if msg.grouped_id == current_group_id and msg.id not in msg_ids:
                                        if self._should_forward_message(msg):
                                            initial_group_msgs.append(msg)
                                            msg_ids.append(msg.id)
                                        else:
                                            # 标记不符合条件的消息为已处理
                                            self.database.execute(
                                                "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
                                                (source_channel_id, str(msg.id))
                                            )
                                            forwarded_ids.add(str(msg.id))
                            
                        except Exception as e:
                            self.logger.error(f"尝试收集完整媒体组时出错: {e}")
                        
                        # 按ID排序确保顺序正确
                        grouped_messages = sorted(initial_group_msgs, key=lambda m: m.id)
                        
                    # 检查媒体组中是否存在不符合条件的消息
                    all_valid = True
                    for msg in grouped_messages:
                        if not self._should_forward_message(msg):
                            all_valid = False
                            self.logger.info(f"媒体组中存在不符合条件的消息，跳过整个组")
                            # 标记整个媒体组为已处理
                            for g_msg in grouped_messages:
                                self.database.execute(
                                    "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
                                    (source_channel_id, str(g_msg.id))
                                )
                                forwarded_ids.add(str(g_msg.id))
                            break
                    
                    if not all_valid:
                        continue
                    
                    self.logger.info(f"准备转发完整媒体组，共 {len(grouped_messages)} 条消息")
                    # 转发媒体组
                    success = await self._forward_message_group(grouped_messages, target_channel_id)
                    
                    # 标记所有消息为已处理
                    for msg in grouped_messages:
                        self.database.execute(
                            "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
                            (source_channel_id, str(msg.id))
                        )
                    
                    if success:
                        self.logger.info("媒体组转发成功")
                        return True
                    else:
                        self.logger.error("媒体组转发失败")
                        return False
                else:
                    # 转发单条消息
                    self.logger.info(f"转发单条消息，ID: {latest_message.id}")
                    success = await self._forward_single_message(latest_message, target_channel_id)
                    
                    # 标记消息为已处理
                    self.database.execute(
                        "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
                        (source_channel_id, str(latest_message.id))
                    )
                    
                    if success:
                        self.logger.info("单条消息转发成功")
                        return True
                    else:
                        self.logger.error("单条消息转发失败")
                        return False
            
            # 如果所有批次都处理完还没找到符合条件的消息
            if not found_valid_message:
                self.logger.info("在所有消息中未找到符合条件的消息")
            
            return found_valid_message
            
        except Exception as e:
            self.logger.error(f"常规转发出错: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False
    
    async def forward_message(self, source_channel_id, message):
        """转发单条指定消息
        
        Args:
            source_channel_id: 源频道ID
            message: 要转发的消息对象
            
        Returns:
            bool: 转发是否成功
        """
        try:
            # 获取源频道和目标频道信息
            source_info = self.database.execute(
                """SELECT channel_id, channel_name, target_channel_id 
                   FROM resource_channels WHERE channel_id = ? AND is_transfer = 1""",
                (source_channel_id,)
            )
            
            if not source_info:
                self.logger.error(f"未找到频道ID为 {source_channel_id} 的可转发频道")
                return False
            
            source_channel_id, source_name, target_channel_id = source_info[0]
            
            if not target_channel_id:
                self.logger.error(f"频道 {source_name} 未设置目标频道")
                return False
                
            # 检查消息是否符合转发条件
            if not self._should_forward_message(message):
                self.logger.info(f"消息 {message.id} 不符合转发条件，跳过")
                return False
                
            self.logger.info(f"开始转发消息 ID: {message.id}")
            
            # 转发消息
            success = await self._forward_single_message(message, target_channel_id)
            
            if success:
                self.logger.info(f"消息 {message.id} 转发成功")
                return True
            else:
                self.logger.error(f"消息 {message.id} 转发失败")
                return False
                
        except Exception as e:
            self.logger.error(f"转发单条消息出错: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False
    
    def _has_media(self, message):
        """检查消息是否包含媒体"""
        return message.media and (
            isinstance(message.media, MessageMediaPhoto) or 
            isinstance(message.media, MessageMediaDocument) or
            isinstance(message.media, MessageMediaWebPage)
        )
    
    def _should_forward_message(self, message):
        """检查消息是否应该被转发"""
        # 跳过纯文本消息（必须有媒体）
        if not self._has_media(message):
            self.logger.info(f"消息 {message.id} 是纯文本消息，不转发")
            return False
            
        # 检查消息是否有按钮
        if hasattr(message, 'reply_markup') and message.reply_markup:
            self.logger.info(f"消息 {message.id} 带有按钮，不转发")
            return False
            
        # 检查消息文本是否包含广告关键词
        if message.message:
            text = message.message.lower()
            for keyword in self.ad_keywords:
                if keyword.lower() in text:
                    self.logger.info(f"消息 {message.id} 包含广告关键词 '{keyword}'，不转发")
                    return False
            
            # 检查消息文本是否包含非超链接形式的URL
            urls = self.url_pattern.findall(text)
            if urls:
                # 检查是否是超链接格式
                is_markdown_link = False
                for url in urls:
                    # 寻找Markdown链接格式: [text](url)
                    link_pattern = r'\[.+?\]\(' + re.escape(url) + r'\)'
                    if re.search(link_pattern, message.message):
                        is_markdown_link = True
                    else:
                        is_markdown_link = False
                        break
                        
                if not is_markdown_link:
                    self.logger.info(f"消息 {message.id} 包含非超链接形式的URL，不转发")
                    return False
        
        # 如果是网页预览，不转发
        if hasattr(message, 'media') and isinstance(message.media, MessageMediaWebPage):
            self.logger.info(f"消息 {message.id} 包含网页预览，不转发")
            return False
            
        # 通过所有检查，可以转发
        return True
    
    async def _forward_single_message(self, message, target_channel_id):
        """转发单条消息"""
        try:
            # 首先尝试直接转发消息
            forward_success = False
            try:
                # 使用转发功能，保留原始格式
                forwarded = await self.user_client.forward_messages(
                    entity=int(target_channel_id),
                    messages=message,
                    drop_author=True  # 不显示原作者
                )
                
                if forwarded:
                    self.logger.info(f"媒体转发成功，新消息ID为：{forwarded.id if hasattr(forwarded, 'id') else '未知'}")
                    forward_success = True
                else:
                    self.logger.warning(f"普通转发消息失败，消息ID：{message.id}，尝试使用限制转发")
            except Exception as e:
                self.logger.warning(f"普通转发消息出错: {str(e)}，尝试使用限制转发")
            
            # 如果直接转发失败，尝试使用限制转发处理器
            if not forward_success:
                self.logger.info(f"检测到转发限制，切换到限制转发模式，消息ID：{message.id}")
                source_channel_id = message.peer_id.channel_id if hasattr(message.peer_id, 'channel_id') else message.chat_id
                success = await self.restricted_handler.forward_message(
                    message,
                    source_channel_id,
                    target_channel_id
                )
                forward_success = success
            
            # 等待一小段时间，避免频繁发送
            await asyncio.sleep(2)
            
            return forward_success
            
        except Exception as e:
            self.logger.error(f"转发单条消息出错: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False
    
    async def _forward_message_group(self, messages, target_channel_id):
        """转发消息组"""
        try:
            if not messages:
                return False
            
            # 确保消息按照正确的顺序排序（按ID从小到大）
            sorted_messages = sorted(messages, key=lambda msg: msg.id)
            
            # 首先尝试直接转发媒体组
            forward_success = False
            try:
                # 使用转发功能，保留原始格式
                forwarded = await self.user_client.forward_messages(
                    entity=int(target_channel_id),
                    messages=sorted_messages,
                    drop_author=True  # 不显示原作者
                )
                
                if forwarded:
                    self.logger.info(f"媒体组转发成功，新消息数量为：{len(forwarded) if isinstance(forwarded, list) else 1}")
                    forward_success = True
                else:
                    self.logger.warning("普通转发媒体组失败，尝试使用限制转发")
            except Exception as e:
                self.logger.warning(f"普通转发媒体组出错: {str(e)}，尝试使用限制转发")
            
            # 如果直接转发失败，尝试使用限制转发处理器
            if not forward_success:
                self.logger.info(f"检测到转发限制，切换到限制转发模式，媒体组消息数量：{len(sorted_messages)}")
                source_channel_id = sorted_messages[0].peer_id.channel_id if hasattr(sorted_messages[0].peer_id, 'channel_id') else sorted_messages[0].chat_id
                success = await self.restricted_handler.forward_message_group(
                    sorted_messages,
                    source_channel_id,
                    target_channel_id
                )
                forward_success = success
            
            # 等待一小段时间，避免频繁发送
            await asyncio.sleep(3)
            
            return forward_success
            
        except Exception as e:
            self.logger.error(f"转发消息组出错: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False 