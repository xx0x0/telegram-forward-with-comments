import asyncio
import time
import hashlib
import inspect
import math
import os
import io
from pathlib import Path
from collections import defaultdict
from typing import Optional, List, AsyncGenerator, Union, DefaultDict, Tuple, BinaryIO, Dict

from telethon import utils, helpers, TelegramClient
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import (GetFileRequest, SaveFilePartRequest,
                                          SaveBigFilePartRequest)
from telethon.tl.types import (Document, InputFileLocation, InputDocumentFileLocation,
                               InputPhotoFileLocation, InputPeerPhotoFileLocation, TypeInputFile,
                               InputFileBig, InputFile)
from utils import setup_logger

logger = setup_logger(__name__)

TypeLocation = Union[Document, InputDocumentFileLocation, InputPeerPhotoFileLocation,
                     InputFileLocation, InputPhotoFileLocation]

class DownloadSender:
    client: TelegramClient
    sender: MTProtoSender
    request: GetFileRequest
    remaining: int
    stride: int

    def __init__(self, client: TelegramClient, sender: MTProtoSender, file: TypeLocation, offset: int, limit: int,
                 stride: int, count: int) -> None:
        self.sender = sender
        self.client = client
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self, max_retries: int = 5, timeout: int = 60):
        """获取下一个数据块，带有智能重试机制"""
        for attempt in range(max_retries):
            try:
                async with asyncio.timeout(timeout):
                    result = await self.client._call(self.sender, self.request)
                    self.remaining -= 1
                    self.request.offset += self.stride
                    return result.bytes
            except (asyncio.TimeoutError, ConnectionError) as e:
                if attempt == max_retries - 1:
                    raise
                # 指数退避重试
                wait_time = min(30, 2 ** attempt)
                logger.warning(f"下载尝试 {attempt + 1} 失败: {str(e)}. {wait_time}秒后重试...")
                await asyncio.sleep(wait_time)
            except Exception as e:
                # 检查是否是频率限制错误
                if "A wait of" in str(e):
                    wait_time = int(str(e).split("wait of ")[1].split(" seconds")[0])
                    logger.info(f"遇到频率限制，等待 {wait_time} 秒")
                    await asyncio.sleep(wait_time + 1)  # 多等1秒以确保安全
                    continue
                raise  # 其他错误直接抛出
        return None

class UploadSender:
    client: TelegramClient
    sender: MTProtoSender
    request: Union[SaveFilePartRequest, SaveBigFilePartRequest]
    part_count: int
    stride: int
    previous: Optional[asyncio.Task]
    loop: asyncio.AbstractEventLoop
    file_part: int

    def __init__(self, client: TelegramClient, sender: MTProtoSender, file_id: int, part_count: int, big: bool,
                 index: int,
                 stride: int, loop: asyncio.AbstractEventLoop) -> None:
        self.client = client
        self.sender = sender
        self.part_count = part_count
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous = None
        self.loop = loop
        self.file_part = index

    async def next(self, data: bytes) -> None:
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data: bytes, max_retries: int = 5) -> None:
        """发送下一个文件块，增加了重试机制"""
        self.request.bytes = data
        
        for attempt in range(max_retries):
            try:
                await self.client._call(self.sender, self.request)
                self.request.file_part += self.stride
                return
            except (ConnectionError, asyncio.TimeoutError) as e:
                if attempt == max_retries - 1:
                    logger.error(f"上传文件块 {self.file_part} 失败，已达到最大重试次数: {str(e)}")
                    raise
                
                # 指数退避重试
                wait_time = min(30, 2 ** attempt)
                logger.warning(f"上传文件块 {self.file_part} 尝试 {attempt + 1} 失败: {str(e)}. {wait_time}秒后重试...")
                await asyncio.sleep(wait_time)
            except Exception as e:
                # 检查是否是频率限制错误
                if "A wait of" in str(e):
                    wait_time = int(str(e).split("wait of ")[1].split(" seconds")[0])
                    logger.info(f"上传频率限制，等待 {wait_time} 秒后重试文件块 {self.file_part}")
                    await asyncio.sleep(wait_time + 1)  # 多等1秒以确保安全
                    continue
                
                # 其他可能的临时错误
                if attempt < max_retries - 1 and ("floodwait" in str(e).lower() or "overload" in str(e).lower()):
                    wait_time = min(60, 5 ** attempt)
                    logger.warning(f"上传文件块 {self.file_part} 遇到临时错误: {str(e)}. {wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                    continue
                    
                # 无法恢复的错误，直接抛出
                logger.error(f"上传文件块 {self.file_part} 遇到严重错误: {str(e)}")
                raise

class ConnectionPool:
    client: TelegramClient
    dc_id: Optional[int]
    max_connections: int
    connections: List[MTProtoSender]
    connection_timestamps: Dict[MTProtoSender, float]
    lock: asyncio.Lock
    auth_key: Optional[AuthKey]
    
    def __init__(self, client: TelegramClient, dc_id: int, max_connections: int = 20):
        self.client = client
        self.dc_id = dc_id
        self.max_connections = max_connections
        self.connections: List[MTProtoSender] = []
        self.connection_timestamps: Dict[MTProtoSender, float] = {}
        self.lock = asyncio.Lock()
        self.auth_key = (None if dc_id and self.client.session.dc_id != dc_id
                         else self.client.session.auth_key)

        self.connection_check_task = None
        self._start_connection_check_timer()

    def _start_connection_check_timer(self):
        async def check_connections():
            while True:
                await asyncio.sleep(300)  
                try:
                    await self._cleanup_inactive_connections()
                except Exception as e:
                    logger.error(f"Error cleaning up connections: {e}")

        self.connection_check_task = asyncio.create_task(check_connections())

    async def _cleanup_inactive_connections(self):
        async with self.lock:
            current_time = time.time()
            inactive_timeout = 300  # 降低到5分钟
            max_idle_connections = 5  # 保留的最大空闲连接数

            # 按最后使用时间排序
            connections = sorted(
                self.connections,
                key=lambda x: self.connection_timestamps.get(x, 0)
            )

            # 保留最近使用的连接
            for conn in connections[max_idle_connections:]:
                if current_time - self.connection_timestamps.get(conn, 0) >= inactive_timeout:
                    try:
                        await conn.disconnect()
                        self.connections.remove(conn)
                        del self.connection_timestamps[conn]
                    except Exception as e:
                        logger.error(f"Error disconnecting connection: {e}")

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(dc.ip_address, dc.port, dc.id,
                                                     loggers=self.client._log,
                                                     proxy=self.client._proxy))
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(id=auth.id,
                                                                         bytes=auth.bytes)
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        
        return sender

    async def get_sender(self, max_allowed_connections: int = 12) -> MTProtoSender:
        async with self.lock:
            current_time = time.time()
            
            # 清理空闲时间过长的连接
            inactive_timeout = 300  # 5分钟
            for conn in list(self.connections):
                if current_time - self.connection_timestamps.get(conn, 0) > inactive_timeout:
                    await conn.disconnect()
                    self.connections.remove(conn)
                    del self.connection_timestamps[conn]
            
            # 复用空闲连接
            for conn in self.connections:
                if current_time - self.connection_timestamps.get(conn, 0) > 60:  # 1分钟未使用
                    self.connection_timestamps[conn] = current_time
                    return conn
            
            # 创建新连接
            if len(self.connections) < max_allowed_connections:
                sender = await self._create_sender()
                self.connections.append(sender)
                self.connection_timestamps[sender] = current_time
                return sender
            
            # 达到最大连接数，复用最早的连接
            oldest_sender = min(
                self.connections, 
                key=lambda x: self.connection_timestamps.get(x, current_time)
            )
            self.connection_timestamps[oldest_sender] = current_time
            return oldest_sender

    async def release_sender(self, sender: MTProtoSender):
        async with self.lock:
            self.connection_timestamps[sender] = time.time()

class RateLimiter:
    def __init__(self, max_bytes_per_second: int = 2 * 1024 * 1024):  # 默认2MB/s
        self.max_bytes_per_second = max_bytes_per_second
        self.current_bytes = 0
        self.last_reset = time.time()
        self.lock = asyncio.Lock()
    
    async def acquire(self, bytes_count: int):
        async with self.lock:
            current_time = time.time()
            time_diff = current_time - self.last_reset
            
            if time_diff >= 1:
                self.current_bytes = 0
                self.last_reset = current_time
            
            while self.current_bytes + bytes_count > self.max_bytes_per_second:
                await asyncio.sleep(0.1)
                
                current_time = time.time()
                time_diff = current_time - self.last_reset
                if time_diff >= 1:
                    self.current_bytes = 0
                    self.last_reset = current_time
            
            self.current_bytes += bytes_count

class ParallelTransferrer:
    _connection_pools: Dict[int, ConnectionPool] = {}
    _pool_lock = asyncio.Lock()
    client: TelegramClient
    loop: asyncio.AbstractEventLoop
    dc_id: int
    senders: Optional[List[Union[DownloadSender, UploadSender]]]
    upload_ticker: int
    rate_limiter: RateLimiter

    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        self.senders = None
        self.upload_ticker = 0
        self.rate_limiter = RateLimiter()
    
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.senders:
            connection_pool = await self.get_connection_pool(self.client, self.dc_id)
            for sender in self.senders:
                await connection_pool.release_sender(sender.sender)
            self.senders = None

    @classmethod
    async def get_connection_pool(cls, client: TelegramClient, dc_id: int) -> ConnectionPool:
        async with cls._pool_lock:
            if dc_id not in cls._connection_pools:
                cls._connection_pools[dc_id] = ConnectionPool(client, dc_id)
            return cls._connection_pools[dc_id]

    @staticmethod
    def _get_connection_count(file_size: int, max_count: int = 20,
                              full_size: int = 100 * 1024 * 1024) -> int:
        """优化连接数计算，减少大文件的连接数，降低出错风险"""
        if file_size <= 1024 * 1024:  # 1MB以下
            return 4
        elif file_size <= 10 * 1024 * 1024:  # 10MB以下
            return 6  # 从8减少到6
        elif file_size <= 100 * 1024 * 1024:  # 100MB以下
            return 8  # 从12减少到8
        else:
            return min(12, max(8, math.ceil(file_size / (50 * 1024 * 1024))))  # 最大连接数从20减少到12

    async def _init_download(self, connections: int, file: TypeLocation, part_count: int,
                           part_size: int) -> None:
        minimum, remainder = divmod(part_count, connections)

        def get_part_count() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        connection_pool = await self.get_connection_pool(self.client, self.dc_id)
        
        self.senders = [
            await self._create_download_sender(connection_pool, file, 0, part_size, connections * part_size,
                                               get_part_count()),
            *await asyncio.gather(
                *[self._create_download_sender(connection_pool, file, i, part_size, connections * part_size,
                                               get_part_count())
                  for i in range(1, connections)])
        ]

    async def _create_download_sender(self, connection_pool: ConnectionPool, file: TypeLocation, index: int, 
                                  part_size: int, stride: int, part_count: int) -> DownloadSender:
        max_connections = 12 if part_size * part_count > 5 * 1024 * 1024 else 8
        sender = await connection_pool.get_sender(max_allowed_connections=max_connections)
        return DownloadSender(self.client, sender, file, index * part_size, part_size,
                            stride, part_count)

    async def download(self, file: TypeLocation, file_size: int,
                       part_size_kb: Optional[float] = None,
                       connection_count: Optional[int] = None) -> AsyncGenerator[bytes, None]:
        """优化下载方法"""
        connection_count = connection_count or self._get_connection_count(file_size)
        
        # 根据文件大小优化分块大小
        if not part_size_kb:
            if file_size <= 1024 * 1024:  # 1MB以下
                part_size_kb = 64
            elif file_size <= 10 * 1024 * 1024:  # 10MB以下
                part_size_kb = 128
            else:
                part_size_kb = 512
        
        part_size = part_size_kb * 1024
        part_count = math.ceil(file_size / part_size)
        
        await self._init_download(connection_count, file, part_count, part_size)

        part = 0
        while part < part_count:
            tasks = []
            for sender in self.senders:
                tasks.append(self.loop.create_task(sender.next()))
            
            for task in tasks:
                data = await task
                if not data:
                    break
                yield data
                part += 1

    async def _init_upload(self, connections: int, file_id: int, part_count: int, big: bool) -> None:
        self.senders = [
            await self._create_upload_sender(file_id, part_count, big, 0, connections),
            *await asyncio.gather(
                *[self._create_upload_sender(file_id, part_count, big, i, connections)
                  for i in range(1, connections)])
        ]

    async def _create_upload_sender(self, file_id: int, part_count: int, big: bool, index: int,
                                    stride: int) -> UploadSender:
        connection_pool = await self.get_connection_pool(self.client, self.dc_id)
        sender = await connection_pool.get_sender()
        return UploadSender(self.client, sender, file_id, part_count, big, index, stride,
                            loop=self.loop)

    async def init_upload(self, file_id: int, file_size: int, part_size_kb: Optional[float] = None,
                        connection_count: Optional[int] = None) -> Tuple[int, int, bool]:
        # 获取连接数，对于大文件减少连接数
        connection_count = connection_count or self._get_connection_count(file_size)
        
        # 根据文件大小调整块大小，增大块大小减少块数
        if not part_size_kb:
            if file_size <= 1024 * 1024:  # 1MB以下
                part_size_kb = 64
            elif file_size <= 10 * 1024 * 1024:  # 10MB以下
                part_size_kb = 128
            else:
                part_size_kb = 512  # 使用更大的块大小
        else:
            part_size_kb = utils.get_appropriated_part_size(file_size)
            
        part_size = part_size_kb * 1024
        part_count = (file_size + part_size - 1) // part_size
        is_large = file_size > 10 * 1024 * 1024
        
        logger.info(f"初始化上传: 文件大小={self._format_size(file_size)}, 块大小={self._format_size(part_size)}, 块数={part_count}, 连接数={connection_count}")
        
        await self._init_upload(connection_count, file_id, part_count, is_large)
        return part_size, part_count, is_large

    def _format_size(self, size_bytes):
        """格式化文件大小为可读形式"""
        if size_bytes < 0:
            return "0B"
        size_names = ("B", "KB", "MB", "GB", "TB")
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024
            i += 1
        return f"{size_bytes:.2f}{size_names[i]}"

    async def upload(self, part: bytes) -> None:
        """上传一个分块，使用轮流方式分配到不同发送器"""
        await self.senders[self.upload_ticker].next(part)
        self.upload_ticker = (self.upload_ticker + 1) % len(self.senders)

    async def finish_upload(self) -> None:
        """确保所有上传任务完成并释放资源"""
        if self.senders:
            # 等待所有进行中的上传任务完成
            pending_tasks = [sender.previous for sender in self.senders if sender.previous]
            if pending_tasks:
                logger.info(f"等待 {len(pending_tasks)} 个进行中的上传任务完成...")
                await asyncio.gather(*pending_tasks)
                
            # 释放连接
            connection_pool = await self.get_connection_pool(self.client, self.dc_id)
            for sender in self.senders:
                await connection_pool.release_sender(sender.sender)
            self.senders = None
            logger.info("所有上传任务已完成，连接已释放")

parallel_transfer_locks: DefaultDict[int, asyncio.Lock] = defaultdict(lambda: asyncio.Lock())

async def download_file(client: TelegramClient,
                        location: TypeLocation,
                        out: BinaryIO,
                        progress_callback: callable = None
                        ) -> BinaryIO:
    if hasattr(location, 'document'):
        size = location.document.size
    elif hasattr(location, 'photo') and hasattr(location.photo.sizes[-1], 'sizes'):
        size = location.photo.sizes[-1].sizes[-1]
    else:
        size = 2400
        
    dc_id, location = utils.get_input_location(location)
    
    downloader = ParallelTransferrer(client, dc_id)
    downloaded = downloader.download(location, size)
    last_callback_time = 0
    callback_interval = 1  # 每秒最多触发一次

    async for x in downloaded:
        out.write(x)
        if progress_callback and time.time() - last_callback_time >= callback_interval:
            last_callback_time = time.time()
            r = progress_callback(out.tell(), size)
            if inspect.isawaitable(r):
                await r

    return out
    
def stream_file(file_to_stream: BinaryIO, chunk_size=64*1024):
    while True:
        data_read = file_to_stream.read(chunk_size)
        if not data_read:
            break
        yield data_read

async def _internal_transfer_to_telegram(
    client: TelegramClient,
    response: Union[str, BinaryIO],
    progress_callback: Optional[callable] = None
) -> Tuple[TypeInputFile, int]:
    if isinstance(response, str):
        file_path = response
        response = open(file_path, 'rb')
        file_name = os.path.basename(file_path)
    else:
        file_name = getattr(response, 'name', 'upload')
        file_path = response.name

    file_id = helpers.generate_random_long()
    file_size = os.path.getsize(file_path)

    hash_md5 = hashlib.md5()
    uploader = ParallelTransferrer(client)
    part_size, part_count, is_large = await uploader.init_upload(file_id, file_size)
    buffer = bytearray()
    upload_count = 0
    
    max_retries = 3  # 整个上传过程的最大重试次数
    
    for retry in range(max_retries):
        try:
            # 重置文件位置和上传计数器
            if retry > 0:
                logger.warning(f"重新尝试上传整个文件 (尝试 {retry+1}/{max_retries})...")
                response.seek(0)
                upload_count = 0
                
                # 重新初始化上传器
                await uploader.finish_upload()
                uploader = ParallelTransferrer(client)
                # 使用不同的文件ID重新开始
                file_id = helpers.generate_random_long()
                part_size, part_count, is_large = await uploader.init_upload(file_id, file_size)
                buffer = bytearray()
                
                if not is_large:
                    hash_md5 = hashlib.md5()
            
            # 上传文件内容
            for data in stream_file(response):
                if progress_callback:
                    r = progress_callback(response.tell(), file_size)
                    if inspect.isawaitable(r):
                        await r
                
                if not is_large:
                    hash_md5.update(data)
                
                if len(buffer) == 0 and len(data) == part_size:
                    await uploader.upload(data)
                    upload_count += 1
                    continue
                
                new_len = len(buffer) + len(data)
                if new_len >= part_size:
                    cutoff = part_size - len(buffer)
                    buffer.extend(data[:cutoff])
                    await uploader.upload(bytes(buffer))
                    upload_count += 1
                    buffer.clear()
                    buffer.extend(data[cutoff:])
                else:
                    buffer.extend(data)
            
            if len(buffer) > 0:
                await uploader.upload(bytes(buffer))
                upload_count += 1
            
            # 检查是否所有分块都已上传
            if upload_count != part_count:
                logger.warning(f"上传块数不匹配：实际 {upload_count} vs 预期 {part_count}, 可能导致错误")
                # 仍然继续处理，可能部分上传是正确的
            
            await uploader.finish_upload()
            
            # 成功完成上传，跳出重试循环
            break
            
        except Exception as e:
            await uploader.finish_upload()
            
            if retry == max_retries - 1:
                logger.error(f"上传文件失败，已达最大重试次数: {str(e)}")
                raise
                
            # 添加一些休眠时间再重试
            wait_time = 5 * (retry + 1)
            logger.warning(f"上传文件失败: {str(e)}. {wait_time}秒后重试...")
            await asyncio.sleep(wait_time)
    
    logger.info(f"完成上传文件: {file_name}, 大小: {uploader._format_size(file_size)}, 分块: {upload_count}/{part_count}")
    
    if is_large:
        return InputFileBig(file_id, part_count, file_name), file_size
    else:
        return InputFile(file_id, part_count, file_name, hash_md5.hexdigest()), file_size

async def upload_file(
    client: TelegramClient,
    file: Union[str, bytes, Path],
    progress_callback: callable = None
) -> TypeInputFile:
    # 对小文件使用非并行上传以减少出错机会
    file_size = 0
    if isinstance(file, (str, Path)):
        file_path = str(file)
        file_size = os.path.getsize(file_path)
    elif isinstance(file, bytes):
        file_size = len(file)
    
    # 文件较小时（小于500KB）使用Telethon原生上传方法
    if file_size > 0 and file_size < 500 * 1024:
        logger.info(f"使用原生上传方法处理小文件: {file_size} 字节")
        if isinstance(file, (str, Path)):
            return await client.upload_file(file, progress_callback=progress_callback)
        else:
            return await client.upload_file(io.BytesIO(file), progress_callback=progress_callback)
    
    # 对较大文件使用优化的并行上传
    if isinstance(file, (str, Path)):
        file_path = str(file)
        with open(file_path, 'rb') as file_handle:
            return (await _internal_transfer_to_telegram(
                client=client,
                response=file_handle,
                progress_callback=progress_callback
            ))[0]
    else:
        with io.BytesIO(file) as file_handle:
            return (await _internal_transfer_to_telegram(
                client=client,
                response=file_handle,
                progress_callback=progress_callback
            ))[0]