from telethon import events, utils
from utils import setup_logger
from .normal_forward_handler import NormalForwardHandler
from .comment_forward_handler import CommentForwardHandler
import asyncio
import traceback
import time
import datetime

class ChannelConfig:
    """频道配置类，用于跟踪每个频道的配置和状态"""
    def __init__(self, channel_id, channel_name, send_type, send_interval):
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.send_type = send_type
        self.send_interval = send_interval
        self.last_forward_time = None  # 首次运行时设为None，表示从未转发过
        
    def update_config(self, channel_name, send_type, send_interval):
        """更新频道配置"""
        self.channel_name = channel_name
        self.send_type = send_type
        self.send_interval = send_interval
        
    def should_forward(self, current_time):
        """检查是否应该转发"""
        # 如果从未转发过，应该立即转发
        if self.last_forward_time is None:
            return True
            
        # 计算经过的时间（小时）
        elapsed_hours = (current_time - self.last_forward_time) / 3600
        
        # 如果超过间隔，应该转发
        return elapsed_hours >= self.send_interval
        
    def update_forward_time(self, current_time):
        """更新最近转发时间"""
        self.last_forward_time = current_time

class AutoForwardScheduler:
    def __init__(self, user_client, database):
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        
        # 初始化转发处理器
        self.normal_handler = NormalForwardHandler(user_client, database)
        self.comment_handler = CommentForwardHandler(user_client, database)
        
        # 运行状态控制
        self.is_running = False
        self.stop_event = asyncio.Event()
        
        # 频道配置集合
        self.channel_configs = {}
        
        # 上次从数据库加载配置的时间
        self.last_config_load_time = 0
        
        # 当前处理的索引
        self.current_index = 0
    
    async def start(self):
        """启动自动转发调度器"""
        if self.is_running:
            self.logger.warning("自动转发调度器已在运行")
            return
            
        self.is_running = True
        self.stop_event.clear()
        
        # 加载初始配置
        await self._load_channel_configs()
        
        # 启动转发循环
        asyncio.create_task(self._forward_loop())
        self.logger.info("自动转发调度器已启动")
    
    async def stop(self):
        """停止自动转发调度器"""
        if not self.is_running:
            return
            
        self.is_running = False
        self.stop_event.set()
        self.logger.info("自动转发调度器已停止")
    
    async def _forward_loop(self):
        """主转发循环"""
        self.logger.info("转发循环启动")

        try:
            while self.is_running:
                # 每轮先检查 db 里的配置变更（用户在 bot 里新加/改频道无需等待）
                await self._check_config_update()

                # 处理一个频道
                processed = await self._process_next_channel()

                if processed:
                    # 处理完一个频道后等待1分钟
                    self.logger.info("等待1分钟再处理下一个频道")
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                else:
                    # 没有可处理的频道：短暂等待后重新检查 db
                    # 比之前的 30s 短，让"刚在 bot 里配的频道"能尽快被拾起
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass

                if self.stop_event.is_set():
                    break

        except asyncio.CancelledError:
            self.logger.info("转发循环被取消")
        except Exception as e:
            self.logger.error(f"转发循环发生错误: {str(e)}")
            self.logger.error(traceback.format_exc())

        self.logger.info("转发循环已停止")
    
    async def _load_channel_configs(self):
        """从数据库加载频道配置"""
        try:
            # 记录加载时间
            self.last_config_load_time = time.time()
            
            # 获取所有开启转发的频道
            channels = self.database.execute(
                "SELECT id, channel_id, channel_name, send_type, send_interval FROM resource_channels WHERE is_transfer = 1"
            )
            
            if not channels:
                self.logger.info("没有配置需要转发的频道")
                self.channel_configs = {}
                return
                
            self.logger.info(f"从数据库加载了 {len(channels)} 个需要转发的频道")
            
            # 临时存储加载的ID，用于检测删除
            loaded_ids = set()
            
            # 更新或添加配置
            for _, channel_id, channel_name, send_type, send_interval in channels:
                # 跳过没有设置发送间隔的频道
                if send_interval is None:
                    self.logger.warning(f"频道 {channel_name} (ID: {channel_id}) 未设置转发间隔，跳过")
                    continue
                    
                loaded_ids.add(channel_id)
                
                if channel_id in self.channel_configs:
                    # 更新现有配置
                    self.channel_configs[channel_id].update_config(
                        channel_name, send_type, send_interval
                    )
                    self.logger.info(f"更新频道配置: {channel_name} (ID: {channel_id})")
                else:
                    # 添加新配置
                    self.channel_configs[channel_id] = ChannelConfig(
                        channel_id, channel_name, send_type, send_interval
                    )
                    self.logger.info(f"添加新频道配置: {channel_name} (ID: {channel_id})")
            
            # 删除不再存在的频道
            removed_ids = []
            for channel_id in list(self.channel_configs.keys()):
                if channel_id not in loaded_ids:
                    removed_ids.append(channel_id)
                    del self.channel_configs[channel_id]
            
            if removed_ids:
                self.logger.info(f"移除了 {len(removed_ids)} 个不再需要转发的频道")
                
            # 重置处理索引
            if self.channel_configs:
                self.current_index = 0
                
        except Exception as e:
            self.logger.error(f"加载频道配置出错: {str(e)}")
            self.logger.error(traceback.format_exc())
    
    async def _check_config_update(self):
        """检查是否需要更新配置（30 秒内不会重复读 db，避免每轮空转都打数据库）"""
        current_time = time.time()
        if current_time - self.last_config_load_time > 30:
            await self._load_channel_configs()
    
    async def _process_next_channel(self):
        """处理下一个需要转发的频道"""
        try:
            if not self.channel_configs:
                self.logger.info("没有配置的频道需要处理")
                return False
                
            # 获取所有频道ID的列表
            channel_ids = list(self.channel_configs.keys())
            
            # 如果当前索引超出范围，重置为0
            if self.current_index >= len(channel_ids):
                self.current_index = 0
                
            # 从当前索引开始，尝试处理符合条件的频道
            start_index = self.current_index
            processed = False
            
            while True:
                channel_id = channel_ids[self.current_index]
                channel_config = self.channel_configs[channel_id]
                
                # 移动到下一个索引，供下次使用
                self.current_index = (self.current_index + 1) % len(channel_ids)
                
                # 检查是否应该转发
                current_time = time.time()
                if channel_config.should_forward(current_time):
                    self.logger.info(f"开始处理频道 {channel_config.channel_name} 的转发任务")
                    
                    # 记录处理开始时间
                    start_time = time.time()
                    
                    # 获取源频道信息和目标频道ID
                    source_info = self.database.execute(
                        """SELECT channel_id, channel_name, target_channel_id 
                           FROM resource_channels WHERE channel_id = ? AND is_transfer = 1""",
                        (channel_id,)
                    )
                    
                    if not source_info:
                        self.logger.error(f"找不到频道 {channel_config.channel_name} 的目标频道信息")
                        # 更新最后转发时间避免频繁检查
                        channel_config.update_forward_time(current_time)
                        break
                        
                    _, _, target_channel_id = source_info[0]
                    
                    if not target_channel_id:
                        self.logger.error(f"频道 {channel_config.channel_name} 未设置目标频道")
                        # 更新最后转发时间避免频繁检查
                        channel_config.update_forward_time(current_time)
                        break
                    
                    # 根据转发类型选择处理器
                    handler = self.comment_handler if channel_config.send_type == 1 else self.normal_handler
                    
                    # 转发消息（每个处理器内部会决定获取哪些消息，并执行转发）
                    success = await handler.forward_messages(channel_id)
                    
                    if success:
                        self.logger.info(f"频道 {channel_config.channel_name} 转发成功")
                    else:
                        self.logger.warning(f"频道 {channel_config.channel_name} 转发失败")
                    
                    # 更新最后转发时间
                    channel_config.update_forward_time(current_time)
                    
                    # 记录处理结束时间
                    end_time = time.time()
                    elapsed = end_time - start_time
                    self.logger.info(f"频道 {channel_config.channel_name} 处理完成，耗时 {elapsed:.2f} 秒")
                    
                    # 记录下次转发时间
                    next_time = datetime.datetime.fromtimestamp(current_time + channel_config.send_interval * 3600)
                    self.logger.info(f"频道 {channel_config.channel_name} 下次转发时间: {next_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    processed = True
                    break
                
                # 如果已经检查了一圈还未找到可处理的频道，退出循环
                if self.current_index == start_index:
                    break
            
            return processed
                    
        except Exception as e:
            self.logger.error(f"处理频道时出错: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False 