from utils import setup_logger, download_file, upload_file
from telethon import types, errors
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename, InputMediaUploadedPhoto, InputMediaUploadedDocument
import asyncio
import os
import time
import tempfile
import shutil
from pathlib import Path

class RestrictedForwardHandler:
    def __init__(self, bot_client, user_client, config):
        self.bot_client = bot_client
        self.user_client = user_client
        self.config = config
        self.logger = setup_logger(__name__)
        
        self.logger.info("初始化 RestrictedForwardHandler...")
        self.logger.info("RestrictedForwardHandler 初始化完成")
    
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
    
    async def forward_messages_with_download(self, source_entity, target_entity, start_id=None, end_id=None, 
                                        custom_text=None, text_mode=None, topic_id=None, progress_msg=None, stop_check=None):
        """
        通过下载再上传的方式处理有转发限制的频道
        
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
            
        Returns:
            dict: {'success': bool, 'count': int, 'error': str, 'method': str, 'last_message_id': int}
        """
        try:
            forward_count = 0  # 成功转发的消息数量
            skipped_messages = 0  # 跳过的消息数量
            total_messages = 0  # 总消息数量
            last_message_id = None  # 最后处理的消息ID
            
            # 创建临时下载目录
            temp_dir = tempfile.mkdtemp(prefix="tg_forward_")
            self.logger.info(f"创建临时下载目录: {temp_dir}")
            
            try:
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
                
                self.logger.info(f"准备下载并转发消息 - 参数: {iter_params}")
                
                # 媒体组处理变量
                current_group_id = None
                group_messages = []
                group_files = []
                
                # 从源获取消息
                async for message in self.user_client.iter_messages(source_entity, **iter_params):
                    # 检查是否需要停止
                    try:
                        should_stop = stop_check() if callable(stop_check) else False
                        if should_stop:
                            self.logger.info(f"收到停止信号，中断转发，最后处理的消息ID: {last_message_id}")
                            break
                    except Exception as e:
                        self.logger.error(f"检查停止标志时出错: {str(e)}")
                        # 出错时不中断转发
                    
                    total_messages += 1
                    last_message_id = message.id  # 更新最后处理的消息ID
                    
                    # 判断是否为服务消息 - 使用类型检查
                    from telethon.tl.patched import MessageService
                    if isinstance(message, MessageService):
                        self.logger.info(f"跳过服务消息 ID: {message.id}, 类型: {type(message).__name__}")
                        skipped_messages += 1
                        continue
                    
                    # 更精确地检查消息是否可以下载
                    if not hasattr(message, 'media') or not message.media:
                        if hasattr(message, 'message') and message.message:
                            # 纯文本消息，直接发送
                            try:
                                # 检查是否需要停止
                                try:
                                    should_stop = stop_check() if callable(stop_check) else False
                                    if should_stop:
                                        self.logger.info(f"收到停止信号，中断转发，最后处理的消息ID: {last_message_id}")
                                        break
                                except Exception as e:
                                    self.logger.error(f"检查停止标志时出错: {str(e)}")
                                    # 出错时不中断转发
                                
                                # 准备最终文本
                                final_text = self._process_text(message.message, custom_text, text_mode)
                                
                                # 发送文本消息到目标频道 - 不使用回复
                                await self.user_client.send_message(
                                    target_entity,
                                    message=final_text
                                )
                                forward_count += 1
                                self.logger.info(f"成功转发纯文本消息 ID: {message.id}")
                                
                                # 更新进度
                                if progress_msg and total_messages % 5 == 0:
                                    await progress_msg.edit(f"正在下载并转发消息，已完成 {forward_count} 条，跳过 {skipped_messages} 条...")
                            except Exception as e:
                                self.logger.error(f"转发纯文本消息失败 ID: {message.id}, 错误: {str(e)}")
                                skipped_messages += 1
                        else:
                            self.logger.info(f"跳过不可下载消息 ID: {message.id}, 类型: {type(message).__name__}")
                            skipped_messages += 1
                        continue
                    
                    # 检查是否属于媒体组
                    if hasattr(message, 'grouped_id') and message.grouped_id:
                        # 检查是否需要停止
                        try:
                            should_stop = stop_check() if callable(stop_check) else False
                            if should_stop:
                                self.logger.info(f"收到停止信号，中断转发，最后处理的消息ID: {last_message_id}")
                                break
                        except Exception as e:
                            self.logger.error(f"检查停止标志时出错: {str(e)}")
                            # 出错时不中断转发
                        
                        # 如果是新的媒体组ID
                        if current_group_id != message.grouped_id:
                            # 处理之前的媒体组(如果有)
                            if group_messages:
                                try:
                                    group_success = await self._process_media_group(
                                        group_messages, 
                                        group_files, 
                                        target_entity, 
                                        custom_text, 
                                        text_mode,
                                        progress_msg
                                    )
                                    if group_success:
                                        forward_count += len(group_messages)
                                    else:
                                        skipped_messages += len(group_messages)
                                except Exception as e:
                                    self.logger.error(f"处理媒体组失败: {str(e)}")
                                    skipped_messages += len(group_messages)
                                
                            # 清空当前组
                            group_messages = []
                            group_files = []
                        
                        # 设置新的组ID
                        current_group_id = message.grouped_id
                    
                        # 下载当前媒体文件
                        file_path = await self._download_media(message, temp_dir, progress_msg)
                        if file_path:
                            self.logger.info(f"成功下载媒体组文件 ID: {message.id} 到 {file_path}")
                            group_messages.append(message)
                            group_files.append(file_path)
                        else:
                            self.logger.warning(f"下载媒体组文件失败 ID: {message.id}")
                            skipped_messages += 1
                    else:
                        # 检查是否需要停止
                        if stop_check and stop_check():
                            self.logger.info("收到停止信号，中断转发")
                            break
                            
                        # 非媒体组消息，先处理之前收集的媒体组(如果有)
                        if group_messages:
                            try:
                                group_success = await self._process_media_group(
                                    group_messages, 
                                    group_files, 
                                    target_entity, 
                                    custom_text, 
                                    text_mode,
                                    progress_msg
                                )
                                if group_success:
                                    forward_count += len(group_messages)
                                else:
                                    skipped_messages += len(group_messages)
                            except Exception as e:
                                self.logger.error(f"处理媒体组失败: {str(e)}")
                                skipped_messages += len(group_messages)
                            
                        # 清空当前组
                        group_messages = []
                        group_files = []
                        current_group_id = None
                    
                        # 下载并处理单个媒体消息
                        file_path = await self._download_media(message, temp_dir, progress_msg)
                        if file_path:
                            try:
                                success = await self._process_single_media(
                                    message, 
                                    file_path, 
                                    target_entity, 
                                    custom_text, 
                                    text_mode,
                                    progress_msg
                                )
                                if success:
                                    forward_count += 1
                                    self.logger.info(f"成功转发单个媒体 ID: {message.id}")
                                else:
                                    skipped_messages += 1
                                    self.logger.warning(f"转发单个媒体失败 ID: {message.id}")
                                
                                # 删除临时文件
                                if os.path.exists(file_path):
                                    os.unlink(file_path)
                                    self.logger.info(f"删除临时文件: {file_path}")
                            except Exception as e:
                                self.logger.error(f"处理单个媒体失败 ID: {message.id}, 错误: {str(e)}")
                                skipped_messages += 1
                        else:
                            self.logger.warning(f"下载媒体文件失败 ID: {message.id}")
                            skipped_messages += 1
                    
                    # 每处理10条消息更新一次进度
                    if progress_msg and total_messages % 10 == 0:
                        await progress_msg.edit(f"正在下载并转发消息，已完成 {forward_count} 条，跳过 {skipped_messages} 条不可转发的消息...")
                
                # 处理最后一个媒体组(如果有)
                if group_messages:
                    try:
                        group_success = await self._process_media_group(
                            group_messages, 
                            group_files, 
                            target_entity, 
                            custom_text, 
                            text_mode,
                            progress_msg
                        )
                        if group_success:
                            forward_count += len(group_messages)
                        else:
                            skipped_messages += len(group_messages)
                    except Exception as e:
                        self.logger.error(f"处理最后一个媒体组失败: {str(e)}")
                        skipped_messages += len(group_messages)
                
                self.logger.info(f"转发完成 - 总消息数: {total_messages}, 成功转发: {forward_count}, 跳过: {skipped_messages}, 最后消息ID: {last_message_id}")
                
                return {
                    'success': True,
                    'count': forward_count,
                    'skipped': skipped_messages,
                    'total': total_messages,
                    'error': None,
                    'method': 'download_reupload',
                    'last_message_id': last_message_id  # 添加最后处理的消息ID
                }
                
            finally:
                # 清理临时目录
                try:
                    shutil.rmtree(temp_dir)
                    self.logger.info(f"清理临时目录: {temp_dir}")
                except Exception as e:
                    self.logger.error(f"清理临时目录失败: {str(e)}")
            
        except Exception as e:
            self.logger.error(f"下载并转发消息时出错: {str(e)}", exc_info=True)
            return {
                'success': False,
                'count': forward_count if 'forward_count' in locals() else 0,
                'skipped': skipped_messages if 'skipped_messages' in locals() else 0,
                'total': total_messages if 'total_messages' in locals() else 0,
                'error': str(e),
                'method': 'download_reupload',
                'last_message_id': last_message_id if 'last_message_id' in locals() else None  # 添加最后处理的消息ID
            }
    
    def _process_text(self, original_text, custom_text, text_mode):
        """处理文本，根据text_mode合并原始文本和自定义文本"""
        if not custom_text:
            return original_text
        
        if text_mode == "replace":
            return custom_text
        elif text_mode == "append":
            return f"{original_text}\n\n{custom_text}" if original_text else custom_text
        else:
            return original_text
    
    async def _download_media(self, message, temp_dir, progress_msg=None):
        """下载媒体文件到临时目录"""
        try:
            # 生成唯一的文件名
            file_name = f"{message.id}_{int(time.time())}_{os.urandom(4).hex()}"
            
            # 获取适当的扩展名
            ext = self._get_file_extension(message)
            file_path = os.path.join(temp_dir, f"{file_name}{ext}")
            
            # 创建进度回调
            start_time = time.time()
            last_update_time = start_time
            last_current = 0
            
            async def progress_callback(current, total):
                nonlocal last_update_time, last_current
                now = time.time()
                time_diff = now - last_update_time
                
                # 每3秒更新一次进度
                if time_diff >= 3.0:
                    bytes_downloaded = current - last_current
                    speed = bytes_downloaded / time_diff if time_diff > 0 else 0
                    percentage = (current / total) * 100 if total > 0 else 0
                    
                    # 生成进度条
                    bar_length = 16
                    filled_length = int(bar_length * current / total) if total > 0 else 0
                    bar = '█' * filled_length + '░' * (bar_length - filled_length)
                    
                    speed_str = self._format_size(speed) + '/s'
                    
                    # 按照用户要求的格式显示进度信息
                    status = (
                        f"📥 下载中\n"
                        f"消息ID: {message.id}\n"
                        f"下载进度: {self._format_size(current)}/{self._format_size(total)} ({percentage:.1f}%)\n"
                        f"速度: {speed_str}\n"
                        f"[{bar}]"
                    )
                    
                    # 如果有进度消息，则更新
                    if progress_msg:
                        try:
                            asyncio.create_task(progress_msg.edit(status))
                        except:
                            pass
                    
                    # 移除日志记录
                    # self.logger.info(f"下载进度: {status}")
                    last_update_time = now
                    last_current = current
            
            # 使用download_file函数下载媒体
            with open(file_path, 'wb') as f:
                await download_file(
                    client=self.user_client,
                    location=message.media,
                    out=f,
                    progress_callback=progress_callback
                )
            
            self.logger.info(f"下载媒体完成: {file_path}")
            
            # 对于大于10MB的视频，创建缩略图
            if ext.lower() in ['.mp4', '.avi', '.mov', '.mkv', '.webm'] and os.path.getsize(file_path) > 10 * 1024 * 1024:
                thumb_path = f"{file_path}_thumb.jpg"
                if await self._create_thumbnail(file_path, thumb_path):
                    self.logger.info(f"为视频创建缩略图: {thumb_path}")
            
            return file_path
        except Exception as e:
            self.logger.error(f"下载媒体失败: {str(e)}")
            return None
            
    def _format_size(self, size_bytes):
        """格式化文件大小"""
        if size_bytes < 0:
            return "0B"
        size_names = ("B", "KB", "MB", "GB", "TB")
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024
            i += 1
        return f"{size_bytes:.2f}{size_names[i]}"
    
    def _get_file_extension(self, message):
        """根据消息类型获取文件扩展名"""
        if not message.media:
            return ""
        
        from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
        
        if isinstance(message.media, MessageMediaPhoto):
            return ".jpg"
        elif isinstance(message.media, MessageMediaDocument):
            mime_type = message.media.document.mime_type
            
            mime_to_ext = {
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/gif': '.gif',
                'video/mp4': '.mp4',
                'video/quicktime': '.mov',
                'video/x-matroska': '.mkv',
                'audio/mpeg': '.mp3',
                'audio/ogg': '.ogg',
                'application/pdf': '.pdf',
                'application/zip': '.zip'
            }
            
            # 首先尝试从MIME类型获取扩展名
            if mime_type in mime_to_ext:
                return mime_to_ext[mime_type]
            
            # 然后尝试从文件名属性获取
            for attr in message.media.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    _, file_ext = os.path.splitext(attr.file_name)
                    if file_ext:
                        return file_ext
            
            # 最后根据MIME类型前缀猜测
            if mime_type.startswith('image/'):
                return '.jpg'
            elif mime_type.startswith('video/'):
                return '.mp4'
            elif mime_type.startswith('audio/'):
                return '.mp3'
            
            # 默认扩展名
            return '.bin'
        else:
            return '.bin'
    
    async def _create_thumbnail(self, video_path, thumb_path):
        """从视频中提取第2帧作为缩略图"""
        try:
            # 使用OpenCV提取视频帧
            import cv2
            
            self.logger.info(f"使用OpenCV从视频提取缩略图: {video_path}")
            
            # 打开视频文件
            cap = cv2.VideoCapture(video_path)
            
            # 检查视频是否成功打开
            if not cap.isOpened():
                self.logger.warning(f"无法打开视频文件: {video_path}")
                return False
                
            # 读取第一帧
            ret, frame = cap.read()
            
            if not ret:
                self.logger.warning(f"无法读取视频第一帧: {video_path}")
                cap.release()
                return False
                
            # 读取第二帧
            ret, frame = cap.read()
            
            if not ret:
                self.logger.warning(f"无法读取视频第二帧，将使用第一帧作为缩略图: {video_path}")
                # 重新打开视频并读取第一帧
                cap.release()
                cap = cv2.VideoCapture(video_path)
                ret, frame = cap.read()
                
                if not ret:
                    self.logger.warning(f"无法读取视频任何帧: {video_path}")
                    cap.release()
                    return False
            
            # 保存帧为图片
            cv2.imwrite(thumb_path, frame)
            
            # 释放视频对象
            cap.release()
            
            if os.path.exists(thumb_path):
                self.logger.info(f"成功创建缩略图: {thumb_path}")
                return True
            else:
                self.logger.warning(f"缩略图文件未创建: {thumb_path}")
                return False
                
        except Exception as e:
            self.logger.error(f"创建缩略图失败: {str(e)}")
            return False
    
    async def _process_single_media(self, message, file_path, target_entity, custom_text, text_mode, progress_msg=None):
        """处理单个媒体文件"""
        try:
            # 准备最终文本
            original_text = message.message if hasattr(message, 'message') else ""
            final_text = self._process_text(original_text, custom_text, text_mode)
            
            # 获取文件扩展名
            _, ext = os.path.splitext(file_path)
            ext = ext.lower()
            
            # 获取原始消息的媒体属性
            attributes = []
            thumb = None
            
            # 检查是否是视频且有缩略图
            is_video = ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']
            if is_video:
                # 获取视频属性
                for attr in message.media.document.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        # 复用原始视频属性
                        attributes.append(DocumentAttributeVideo(
                            duration=attr.duration,
                            w=attr.w,
                            h=attr.h,
                            supports_streaming=True
                        ))
                        break
                
                # 添加文件名属性
                attributes.append(DocumentAttributeFilename(os.path.basename(file_path)))
                
                # 检查是否有缩略图
                thumb_path = f"{file_path}_thumb.jpg"
                if os.path.exists(thumb_path):
                    thumb = thumb_path
                    self.logger.info(f"使用缩略图: {thumb_path}")
            
            # 创建上传进度回调
            start_time = time.time()
            last_update_time = start_time
            last_current = 0
            
            async def upload_progress_callback(current, total):
                nonlocal last_update_time, last_current
                now = time.time()
                time_diff = now - last_update_time
                
                # 每3秒更新一次进度
                if time_diff >= 3.0:
                    bytes_uploaded = current - last_current
                    speed = bytes_uploaded / time_diff if time_diff > 0 else 0
                    percentage = (current / total) * 100 if total > 0 else 0
                    
                    # 生成进度条
                    bar_length = 16
                    filled_length = int(bar_length * current / total) if total > 0 else 0
                    bar = '█' * filled_length + '░' * (bar_length - filled_length)
                    
                    speed_str = self._format_size(speed) + '/s'
                    
                    # 按照用户要求的格式显示进度信息
                    status = (
                        f"📤 上传中\n"
                        f"消息ID: {message.id}\n"
                        f"下载进度: {self._format_size(current)}/{self._format_size(total)} ({percentage:.1f}%)\n"
                        f"速度: {speed_str}\n"
                        f"[{bar}]"
                    )
                    
                    # 如果有进度消息，则更新
                    if progress_msg:
                        try:
                            asyncio.create_task(progress_msg.edit(status))
                        except:
                            pass
                    
                    # 移除日志记录
                    # self.logger.info(f"上传进度: {status}")
                    last_update_time = now
                    last_current = current
            
            # 上传文件
            uploaded_file = await upload_file(
                self.user_client,
                file_path,
                progress_callback=upload_progress_callback
            )
            
            # 根据文件类型发送，不使用回复功能
            if is_video:
                await self.user_client.send_file(
                    target_entity,
                    file=uploaded_file,
                    caption=final_text,
                    thumb=thumb,
                    attributes=attributes
                )
            else:
                await self.user_client.send_file(
                    target_entity,
                    file=uploaded_file,
                    caption=final_text
                )
            
            return True
        except Exception as e:
            self.logger.error(f"处理单个媒体失败: {str(e)}")
            return False
    
    async def _process_media_group(self, messages, file_paths, target_entity, custom_text, text_mode, progress_msg=None):
        """处理媒体组"""
        try:
            if not messages or not file_paths or len(messages) != len(file_paths):
                self.logger.error(f"媒体组处理参数不匹配: 消息 {len(messages)}, 文件 {len(file_paths)}")
                return False
            
            # 准备媒体组
            media_files_to_send = []
            
            # 处理文本 - 只使用第一条消息的文本
            original_text = messages[0].message if hasattr(messages[0], 'message') else ""
            final_text = self._process_text(original_text, custom_text, text_mode)
            
            # 处理每个文件
            successfully_processed_files = 0
            
            for i, (message, file_path) in enumerate(zip(messages, file_paths)):
                try:
                    # 获取文件扩展名
                    _, ext = os.path.splitext(file_path)
                    ext = ext.lower()
                    
                    # 创建上传进度回调
                    start_time = time.time()
                    last_update_time = start_time
                    last_current = 0
                    
                    async def upload_progress_callback(current, total):
                        nonlocal last_update_time, last_current
                        now = time.time()
                        time_diff = now - last_update_time
                        
                        # 每3秒更新一次进度
                        if time_diff >= 3.0:
                            bytes_uploaded = current - last_current
                            speed = bytes_uploaded / time_diff if time_diff > 0 else 0
                            percentage = (current / total) * 100 if total > 0 else 0
                            
                            # 生成进度条
                            bar_length = 16
                            filled_length = int(bar_length * current / total) if total > 0 else 0
                            bar = '█' * filled_length + '░' * (bar_length - filled_length)
                            
                            speed_str = self._format_size(speed) + '/s'
                            
                            # 按照用户要求的格式显示进度信息
                            status = (
                                f"📤 上传媒体组\n"
                                f"消息ID: {message.id} [{i+1}/{len(messages)}]\n"
                                f"下载进度: {self._format_size(current)}/{self._format_size(total)} ({percentage:.1f}%)\n"
                                f"速度: {speed_str}\n"
                                f"[{bar}]"
                            )
                            
                            # 如果有进度消息，则更新
                            if progress_msg:
                                try:
                                    asyncio.create_task(progress_msg.edit(status))
                                except:
                                    pass
                            
                            # 移除日志记录
                            # self.logger.info(f"上传进度: {status}")
                            last_update_time = now
                            last_current = current
                    
                    # 根据文件类型处理
                    if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                        # 图片
                        uploaded_file = await upload_file(
                            self.user_client, 
                            file_path,
                            progress_callback=upload_progress_callback
                        )
                        
                        # 这里对于第一张图片，在发送时才设置caption，现在不需要设置
                        media = InputMediaUploadedPhoto(
                            file=uploaded_file
                        )
                        media_files_to_send.append(media)
                        successfully_processed_files += 1
                    else:
                        # 视频或其他文件
                        attributes = []
                        thumb_file = None
                        
                        # 如果是视频，获取视频属性
                        if ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']:
                            # 从原始消息中获取视频属性
                            for attr in message.media.document.attributes:
                                if isinstance(attr, DocumentAttributeVideo):
                                    attributes.append(DocumentAttributeVideo(
                                        duration=attr.duration,
                                        w=attr.w,
                                        h=attr.h,
                                        supports_streaming=True
                                    ))
                                    break
                            
                            # 添加文件名属性
                            attributes.append(DocumentAttributeFilename(os.path.basename(file_path)))
                            
                            # 检查是否有缩略图
                            thumb_path = f"{file_path}_thumb.jpg"
                            if os.path.exists(thumb_path):
                                thumb_file = await upload_file(self.user_client, thumb_path)
                                self.logger.info(f"上传缩略图: {thumb_path}")
                        else:
                            # 其他文件类型
                            attributes.append(DocumentAttributeFilename(os.path.basename(file_path)))
                        
                        # 上传文件
                        uploaded_file = await upload_file(
                            self.user_client, 
                            file_path,
                            progress_callback=upload_progress_callback
                        )
                        
                        # 创建媒体对象，注意不设置caption
                        media = InputMediaUploadedDocument(
                            file=uploaded_file,
                            thumb=thumb_file,
                            mime_type=self._get_mime_type(ext),
                            attributes=attributes
                        )
                        media_files_to_send.append(media)
                        successfully_processed_files += 1
                    
                    # 删除临时文件
                    if os.path.exists(file_path):
                        os.unlink(file_path)
                        self.logger.info(f"删除临时文件: {file_path}")
                    
                    # 删除缩略图文件
                    thumb_path = f"{file_path}_thumb.jpg"
                    if os.path.exists(thumb_path):
                        os.unlink(thumb_path)
                        self.logger.info(f"删除临时缩略图: {thumb_path}")
                    
                except Exception as e:
                    self.logger.error(f"处理媒体组文件失败: {file_path}, 错误: {str(e)}")
                    # 继续处理其他文件
            
            # 发送媒体组，增加错误处理
            if media_files_to_send:
                try:
                    # 发送媒体组时，单独设置caption
                    await self.user_client.send_file(
                        target_entity,
                        media_files_to_send,
                        caption=final_text
                    )
                    return successfully_processed_files > 0
                except Exception as e:
                    self.logger.error(f"发送媒体组失败: {str(e)}")
                    
                    # 如果媒体组发送失败，尝试单独发送每个媒体
                    if len(media_files_to_send) > 1:
                        self.logger.info("尝试单独发送媒体组中的每个文件")
                        success_count = 0
                        
                        for i, media in enumerate(media_files_to_send):
                            try:
                                # 只给第一个媒体添加文本
                                file_caption = final_text if i == 0 else ""
                                
                                if isinstance(media, InputMediaUploadedPhoto):
                                    await self.user_client.send_file(
                                        target_entity,
                                        media.file,
                                        caption=file_caption
                                    )
                                    success_count += 1
                                elif isinstance(media, InputMediaUploadedDocument):
                                    await self.user_client.send_file(
                                        target_entity,
                                        media.file,
                                        thumb=media.thumb,
                                        attributes=media.attributes,
                                        caption=file_caption
                                    )
                                    success_count += 1
                            except Exception as single_e:
                                self.logger.error(f"单独发送媒体失败: {str(single_e)}")
                                
                        return success_count > 0
                    return False
            else:
                self.logger.warning("没有有效的媒体文件可发送")
                return False
        
        except Exception as e:
            self.logger.error(f"处理媒体组失败: {str(e)}")
            return False
    
    def _get_mime_type(self, ext):
        """根据文件扩展名获取MIME类型"""
        ext_to_mime = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.mp4': 'video/mp4',
            '.mov': 'video/quicktime',
            '.mkv': 'video/x-matroska',
            '.avi': 'video/x-msvideo',
            '.mp3': 'audio/mpeg',
            '.ogg': 'audio/ogg',
            '.pdf': 'application/pdf',
            '.zip': 'application/zip',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        }
        
        return ext_to_mime.get(ext.lower(), 'application/octet-stream') 