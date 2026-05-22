import re
import asyncio
from telethon import types, errors
from telethon.tl.functions.channels import JoinChannelRequest
from utils import setup_logger

class ForwardHandler:
    def __init__(self, bot_client, user_client, config):
        self.bot_client = bot_client
        self.user_client = user_client
        self.config = config
        self.logger = setup_logger(__name__)
        
        self.logger.info("初始化 ForwardHandler...")
        self.logger.info("ForwardHandler 初始化完成")
    
    async def get_entity_from_link(self, link):
        """从链接中获取实体（频道/群组）"""
        try:
            # 检查链接格式
            if link.startswith('https://t.me/'):
                # 普通公开频道/群组
                if '/c/' in link:
                    # 私密群组/频道 (https://t.me/c/12345678/123)
                    parts = link.split('/c/')
                    if len(parts) == 2:
                        channel_data = parts[1].split('/')
                        if len(channel_data) >= 1:
                            channel_id = int(channel_data[0])
                            try:
                                # 尝试通过ID获取私密频道
                                return await self.user_client.get_entity(types.PeerChannel(channel_id))
                            except:
                                self.logger.error(f"无法通过ID获取私密频道: {channel_id}")
                                return None
                else:
                    # 公开频道/群组 (https://t.me/channelname)
                    username = link.split('/')[-1]
                    if username:
                        try:
                            # 尝试获取公开频道/群组
                            entity = await self.user_client.get_entity(username)
                            # 如果还没加入频道，尝试加入
                            try:
                                if isinstance(entity, types.Channel):
                                    await self.user_client(JoinChannelRequest(entity))
                                    self.logger.info(f"已加入频道: {username}")
                            except Exception as e:
                                self.logger.warning(f"加入频道时出现非关键错误: {str(e)}")
                            return entity
                        except:
                            self.logger.error(f"无法获取公开频道/群组: {username}")
                            return None
            
            return None
        except Exception as e:
            self.logger.error(f"获取实体时出错: {str(e)}", exc_info=True)
            return None
            
    async def forward_messages_with_text(self, source_entity, target_entity, start_id=None, end_id=None, 
                                   custom_text=None, text_mode=None, topic_id=None, progress_msg=None, stop_check=None, max_errors=None):
        """
        带自定义文本的转发消息
        
        Args:
            source_entity: 源频道/群组
            target_entity: 目标频道/群组
            start_id: 起始消息ID
            end_id: 结束消息ID
            custom_text: 自定义文本
            text_mode: 文本模式 (replace/append)
            topic_id: 话题ID
            progress_msg: 用于更新进度的消息
            stop_check: 检查是否需要停止的函数
            max_errors: 最大允许错误次数，达到此数量后认为频道有转发限制
            
        Returns:
            dict: {'success': bool, 'count': int, 'error': str, 'method': str, 'last_message_id': int}
        """
        try:
            forward_count = 0  # 成功转发的消息数量
            skipped_messages = 0  # 跳过的消息数量
            total_messages = 0  # 总消息数量
            current_group_id = None
            group_messages = []
            protect_error_count = 0  # 保护错误计数
            last_message_id = None  # 最后处理的消息ID
            
            # 构建获取消息的参数
            iter_params = {
                'reverse': True  # 从旧到新获取
            }
            
            # 如果提供了话题ID，添加到参数中
            if topic_id is not None:
                iter_params['reply_to'] = int(topic_id)
            
            # 确保 start_id 和 end_id 是整数或 None
            start_id = int(start_id) if start_id is not None else None
            end_id = int(end_id) if end_id is not None else None
            
            if start_id is not None and end_id is not None:
                # 指定起始和结束ID
                iter_params['min_id'] = min(start_id, end_id) - 1  # -1 是为了包含起始ID
                iter_params['max_id'] = max(start_id, end_id) + 1  # +1 是为了包含结束ID
                self.logger.info(f"转发指定范围消息: 从ID {min(start_id, end_id)} 到 {max(start_id, end_id)}")
            elif start_id is not None:
                # 只指定起始ID，转到最新的
                iter_params['min_id'] = start_id - 1
                self.logger.info(f"转发从ID {start_id} 到最新消息")
            elif end_id is not None:
                # 只指定结束ID，从第一条消息开始
                iter_params['max_id'] = end_id + 1
                self.logger.info(f"转发从最早消息到ID {end_id}")
            else:
                # 如果都是None，不添加任何ID过滤参数，转发全部内容
                self.logger.info("转发频道全部内容")
            
            self.logger.info(f"准备转发消息 - 参数: {iter_params}")
            
            # 从源获取消息
            async for message in self.user_client.iter_messages(source_entity, **iter_params):
                # 更新最后处理的消息ID
                last_message_id = message.id
                total_messages += 1
                
                # 检查是否需要停止
                try:
                    should_stop = stop_check() if callable(stop_check) else False
                    if should_stop:
                        self.logger.info(f"收到停止信号，中断转发，最后处理的消息ID: {last_message_id}")
                        break
                except Exception as e:
                    self.logger.error(f"检查停止标志时出错: {str(e)}")
                    # 出错时不中断转发
                
                # 检查是否达到最大错误次数
                if max_errors and protect_error_count >= max_errors:
                    self.logger.warning(f"转发保护错误达到最大次数 {max_errors}，中断转发")
                    raise Exception("检测到频道有转发限制，无法继续使用普通转发方式")
                
                # 判断是否为服务消息 - 使用类型检查
                from telethon.tl.patched import MessageService
                if isinstance(message, MessageService):
                    self.logger.info(f"跳过服务消息 ID: {message.id}, 类型: {type(message).__name__}")
                    skipped_messages += 1
                    continue
                
                # 更精确地检查消息是否可以转发
                if not hasattr(message, 'message') and not hasattr(message, 'media'):
                    self.logger.info(f"跳过不可转发消息 ID: {message.id}, 类型: {type(message).__name__}")
                    skipped_messages += 1
                    continue
                
                # 检查消息是否属于媒体组
                if hasattr(message, 'grouped_id') and message.grouped_id:
                    # 如果是新的媒体组ID
                    if current_group_id != message.grouped_id:
                        # 处理之前的媒体组(如果有)
                        if group_messages:
                            try:
                                group_count = await self._send_media_group(group_messages, target_entity, custom_text, text_mode)
                                forward_count += group_count
                            except Exception as e:
                                if "protected chat" in str(e).lower() or "restrict" in str(e).lower() or "forward" in str(e).lower():
                                    protect_error_count += 1
                                    self.logger.warning(f"媒体组转发保护错误 #{protect_error_count}: {str(e)}")
                                    if max_errors and protect_error_count >= max_errors:
                                        raise Exception("检测到频道有转发限制，无法继续使用普通转发方式")
                                else:
                                    self.logger.error(f"发送媒体组时出现其他错误: {str(e)}")
                                    
                                skipped_messages += len(group_messages)
                            
                            # 每次发送后等待2秒
                            await asyncio.sleep(2)
                            # 清空当前组
                            group_messages = []
                        
                        # 设置新的组ID
                        current_group_id = message.grouped_id
                    
                    # 添加消息到当前媒体组
                    group_messages.append(message)
                    
                else:
                    # 非媒体组消息，先处理之前收集的媒体组(如果有)
                    if group_messages:
                        try:
                            group_count = await self._send_media_group(group_messages, target_entity, custom_text, text_mode)
                            forward_count += group_count
                        except Exception as e:
                            if "protected chat" in str(e).lower() or "restrict" in str(e).lower() or "forward" in str(e).lower():
                                protect_error_count += 1
                                self.logger.warning(f"媒体组转发保护错误 #{protect_error_count}: {str(e)}")
                                if max_errors and protect_error_count >= max_errors:
                                    raise Exception("检测到频道有转发限制，无法继续使用普通转发方式")
                            else:
                                self.logger.error(f"发送媒体组时出现其他错误: {str(e)}")
                                
                            skipped_messages += len(group_messages)
                        
                        # 每次发送后等待2秒
                        await asyncio.sleep(2)
                        # 清空当前组
                        group_messages = []
                        current_group_id = None
                    
                    # 处理单个消息
                    try:
                        success = await self._send_single_message(message, target_entity, custom_text, text_mode)
                        if success:
                            forward_count += 1
                        else:
                            skipped_messages += 1
                    except Exception as e:
                        if "protected chat" in str(e).lower() or "restrict" in str(e).lower() or "forward" in str(e).lower():
                            protect_error_count += 1
                            self.logger.warning(f"单条消息转发保护错误 #{protect_error_count}: {str(e)}")
                            if max_errors and protect_error_count >= max_errors:
                                raise Exception("检测到频道有转发限制，无法继续使用普通转发方式")
                        
                        skipped_messages += 1
                    
                    # 每条消息转发后等待2秒
                    await asyncio.sleep(2)
                
                # 每处理10条消息更新一次进度
                if progress_msg and total_messages % 10 == 0:
                    await progress_msg.edit(f"正在转发消息，已完成 {forward_count} 条，跳过 {skipped_messages} 条不可转发的消息...")
                    
            # 处理最后一个媒体组(如果有)
            if group_messages:
                try:
                    group_count = await self._send_media_group(group_messages, target_entity, custom_text, text_mode)
                    forward_count += group_count
                except Exception as e:
                    if "protected chat" in str(e).lower() or "restrict" in str(e).lower() or "forward" in str(e).lower():
                        self.logger.warning(f"最后媒体组转发保护错误: {str(e)}")
                    else:
                        self.logger.error(f"发送最后媒体组时出现其他错误: {str(e)}")
                        
                    skipped_messages += len(group_messages)
            
            self.logger.info(f"转发完成 - 总消息数: {total_messages}, 成功转发: {forward_count}, 跳过: {skipped_messages}, 最后消息ID: {last_message_id}")
            
            return {
                'success': True,
                'count': forward_count,
                'skipped': skipped_messages,
                'total': total_messages,
                'error': None,
                'method': 'normal_forward',
                'last_message_id': last_message_id
            }
            
        except Exception as e:
            self.logger.error(f"转发消息时出错: {str(e)}", exc_info=True)
            return {
                'success': False,
                'count': forward_count if 'forward_count' in locals() else 0,
                'skipped': skipped_messages if 'skipped_messages' in locals() else 0,
                'total': total_messages if 'total_messages' in locals() else 0,
                'error': str(e),
                'method': 'normal_forward',
                'last_message_id': last_message_id if 'last_message_id' in locals() else None
            }
    
    async def _send_single_message(self, message, target_entity, custom_text, text_mode):
        """发送单条消息"""
        try:
            # 准备最终文本
            original_text = message.message if hasattr(message, 'message') else ""
            final_text = original_text
            
            # 如果需要修改文本
            if custom_text and hasattr(message, 'message'):
                # 根据模式决定新文本
                if text_mode == "replace":
                    final_text = custom_text
                elif text_mode == "append":
                    # 添加空行再拼接
                    final_text = f"{original_text}\n\n{custom_text}" if original_text else custom_text
            
            # 直接使用send_file发送媒体或文本，而不是先转发后编辑
            if hasattr(message, 'media') and message.media:
                # 有媒体的消息
                result = await self.user_client.send_file(
                    target_entity,
                    file=message.media,
                    caption=final_text,
                    parse_mode='html'
                )
            else:
                # 纯文本消息
                result = await self.user_client.send_message(
                    target_entity,
                    message=final_text
                )
            
            if result:
                self.logger.info(f"成功发送单条消息，原始ID: {message.id}")
                return True
            return False
        except Exception as e:
            # 对于保护限制相关错误，需要向上传递
            if "protected chat" in str(e).lower() or "restrict" in str(e).lower() or "forward" in str(e).lower():
                self.logger.warning(f"发送单条消息发现转发保护: {str(e)}")
                raise  # 重新抛出异常
            else:
                self.logger.warning(f"发送单条消息失败: {str(e)}")
                return False
    
    async def _send_media_group(self, group_messages, target_entity, custom_text, text_mode):
        """发送媒体组消息，返回成功发送的媒体数量"""
        try:
            self.logger.info(f"处理媒体组, 包含 {len(group_messages)} 条消息")
            
            # 提取媒体文件
            media_files = []
            original_caption = ""
            
            # 找出第一条有文本的消息作为caption
            for msg in group_messages:
                if msg.media:
                    media_files.append(msg.media)
                
                # 获取第一条有文本的消息作为caption
                if not original_caption and hasattr(msg, 'message') and msg.message:
                    original_caption = msg.message
            
            # 处理自定义文本
            final_caption = original_caption
            if custom_text:
                if text_mode == "replace":
                    final_caption = custom_text
                elif text_mode == "append":
                    # 添加空行再拼接
                    final_caption = f"{original_caption}\n\n{custom_text}" if original_caption else custom_text
            
            # 使用send_file直接发送媒体组
            sent_messages = await self.user_client.send_file(
                entity=target_entity,
                file=media_files,
                caption=final_caption,
                parse_mode='html',
                grouped=True
            )
            
            # 计算成功发送的媒体数量
            success_count = len(media_files)
            if isinstance(sent_messages, list):
                self.logger.info(f"成功发送媒体组，包含 {len(sent_messages)} 个文件")
            else:
                self.logger.info(f"成功发送单个媒体文件")
                success_count = 1
            
            return success_count
        except Exception as e:
            # 对于保护限制相关错误，需要向上传递
            if "protected chat" in str(e).lower() or "restrict" in str(e).lower() or "forward" in str(e).lower():
                self.logger.warning(f"发送媒体组发现转发保护: {str(e)}")
                raise  # 重新抛出异常
            else:
                self.logger.error(f"发送媒体组时出错: {str(e)}", exc_info=True)
                return 0
    
    async def forward_messages(self, source_entity, target_entity, start_id=None, end_id=None, progress_msg=None):
        """原始的转发方法，保留用于兼容性"""
        # 调用新的方法，但不使用自定义文本功能
        return await self.forward_messages_with_text(
            source_entity, target_entity, start_id, end_id, 
            custom_text=None, text_mode=None, topic_id=None, progress_msg=progress_msg
        ) 