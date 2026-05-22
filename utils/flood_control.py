"""
统一的限流 / FloodWait 自愈 / 内存流中转工具。

设计目标：
- 所有 user-account 的写操作都过 `safe_call`，自动处理 FloodWaitError
- 用 token bucket 控制全局发消息节奏，避免主动撞限速
- 提供 `stream_media()` 把媒体下到内存，大文件回落到系统 temp
"""
import asyncio
import io
import os
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Optional, Tuple

from telethon.errors import FloodWaitError, SlowModeWaitError, RpcCallFailError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

from .logger import setup_logger

logger = setup_logger(__name__)

# 默认值——50MB 以内走内存，超过的回落到 temp 文件
DEFAULT_MEMORY_THRESHOLD = 50 * 1024 * 1024

# user 账号每分钟最多发送的消息数（保守值，Telegram 实际阈值约 20-30/min）
DEFAULT_RATE_PER_MINUTE = 18


class TokenBucket:
    """简单的 token bucket：每 60/rate 秒生成一个令牌，没令牌就 await。"""

    def __init__(self, rate_per_minute: int = DEFAULT_RATE_PER_MINUTE):
        self.interval = 60.0 / max(rate_per_minute, 1)
        self._lock = asyncio.Lock()
        self._next_available = 0.0

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._next_available - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_available = now + self.interval


class RateLimiter:
    """组合 token bucket + 并发信号量。所有需要节流的协程过这一层。"""

    def __init__(self, rate_per_minute: int = DEFAULT_RATE_PER_MINUTE, concurrency: int = 3):
        self.bucket = TokenBucket(rate_per_minute)
        self.semaphore = asyncio.Semaphore(concurrency)

    @asynccontextmanager
    async def slot(self):
        async with self.semaphore:
            await self.bucket.acquire()
            yield


async def safe_call(coro_factory, *, max_retries: int = 5, rate_limiter: Optional[RateLimiter] = None):
    """
    执行一个 coroutine，自动处理 FloodWait/SlowMode/瞬态网络错误。

    参数:
        coro_factory: 一个无参可调用，返回 coroutine。每次重试都重新调用，
                      因为 coroutine 不能 await 两次。
        max_retries: FloodWait/SlowMode 自动重试上限
        rate_limiter: 可选的 RateLimiter，传入后会在每次实际调用前 acquire
    """
    attempt = 0
    while True:
        try:
            if rate_limiter is not None:
                async with rate_limiter.slot():
                    return await coro_factory()
            else:
                return await coro_factory()
        except FloodWaitError as e:
            attempt += 1
            wait = e.seconds + 2
            if attempt > max_retries:
                logger.error(f"FloodWait 重试 {max_retries} 次仍未恢复，放弃: {e.seconds}s")
                raise
            logger.warning(f"FloodWait {e.seconds}s，等待后重试（第 {attempt}/{max_retries} 次）")
            await asyncio.sleep(wait)
        except SlowModeWaitError as e:
            wait = getattr(e, "seconds", 5) + 1
            logger.warning(f"SlowModeWait {wait}s，等待")
            await asyncio.sleep(wait)
        except RpcCallFailError as e:
            attempt += 1
            if attempt > max_retries:
                raise
            backoff = min(2 ** attempt, 30)
            logger.warning(f"RPC 瞬态错误 {e}，{backoff}s 后重试")
            await asyncio.sleep(backoff)


def _media_size(message) -> int:
    """估算媒体大小，拿不到时返回 0。"""
    media = getattr(message, "media", None)
    if not media:
        return 0
    if isinstance(media, MessageMediaPhoto):
        try:
            sizes = media.photo.sizes
            return max((getattr(s, "size", 0) or 0) for s in sizes) if sizes else 0
        except Exception:
            return 0
    if isinstance(media, MessageMediaDocument):
        try:
            return media.document.size or 0
        except Exception:
            return 0
    return 0


def _media_extension(message) -> str:
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return ".jpg"
    if isinstance(media, MessageMediaDocument):
        mime_to_ext = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
            "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/webm": ".webm",
            "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
            "application/pdf": ".pdf", "application/zip": ".zip",
        }
        mime = getattr(media.document, "mime_type", "") or ""
        if mime in mime_to_ext:
            return mime_to_ext[mime]
        for attr in getattr(media.document, "attributes", []):
            fn = getattr(attr, "file_name", None)
            if fn:
                _, ext = os.path.splitext(fn)
                if ext:
                    return ext
        if mime.startswith("video/"):
            return ".mp4"
        if mime.startswith("image/"):
            return ".jpg"
        if mime.startswith("audio/"):
            return ".mp3"
        return ".bin"
    return ".bin"


class StreamedMedia:
    """
    一个统一的"已下载到本地（可能是内存）"媒体容器。
    用 async with 用：退出时自动清理临时文件。
    """

    def __init__(self, file_obj, *, path: Optional[str] = None, name: str = "", size: int = 0):
        self.file = file_obj
        self.path = path
        self.name = name
        self.size = size

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.file is not None and not isinstance(self.file, str):
                try:
                    self.file.close()
                except Exception:
                    pass
            if self.path and os.path.exists(self.path):
                try:
                    os.unlink(self.path)
                except OSError as e:
                    logger.warning(f"删除临时文件失败 {self.path}: {e}")
        except Exception as e:
            logger.warning(f"清理 StreamedMedia 出错: {e}")


async def stream_media(client, message, *, memory_threshold: int = DEFAULT_MEMORY_THRESHOLD) -> StreamedMedia:
    """
    把消息的媒体下载到内存（小文件）或系统 temp（大文件）。

    返回 StreamedMedia，用 async with 包起来即可在退出时清理。
    """
    size = _media_size(message)
    ext = _media_extension(message)
    name = f"{message.id}{ext}"

    if 0 < size <= memory_threshold or size == 0:
        buf = io.BytesIO()
        buf.name = name
        await client.download_media(message, file=buf)
        buf.seek(0)
        actual = buf.getbuffer().nbytes
        logger.debug(f"媒体 {name} 已下载到内存 ({actual} bytes)")
        return StreamedMedia(buf, path=None, name=name, size=actual)

    # 大文件：放系统 temp，绝不放项目目录
    fd, tmp_path = tempfile.mkstemp(prefix="tg_", suffix=ext)
    os.close(fd)
    try:
        await client.download_media(message, file=tmp_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
    actual = os.path.getsize(tmp_path)
    logger.debug(f"媒体 {name} 已下载到 temp ({actual} bytes): {tmp_path}")
    f = open(tmp_path, "rb")
    return StreamedMedia(f, path=tmp_path, name=name, size=actual)


def make_thumbnail(streamed: StreamedMedia) -> Optional[Tuple[io.BytesIO, str]]:
    """
    给视频生成缩略图（第 2 帧）。返回 (BytesIO, name) 或 None。
    BytesIO 不需要用户清理，留在内存即可。
    cv2 必须读路径，所以只在 streamed.path 不为空时工作。
    内存中的视频会先短暂落 temp 取完帧再删。
    """
    try:
        import cv2
    except ImportError:
        logger.debug("opencv-python 未安装，跳过缩略图")
        return None

    cleanup_path = None
    src_path = streamed.path
    try:
        if src_path is None:
            # BytesIO 中的视频：暂时落到 temp 取帧
            fd, src_path = tempfile.mkstemp(prefix="tg_thumb_src_", suffix=".mp4")
            os.close(fd)
            cleanup_path = src_path
            current_pos = streamed.file.tell()
            streamed.file.seek(0)
            with open(src_path, "wb") as out:
                while True:
                    chunk = streamed.file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            streamed.file.seek(current_pos)

        cap = cv2.VideoCapture(src_path)
        if not cap.isOpened():
            return None
        try:
            cap.read()
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()
            if not ret:
                return None
            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                return None
            buf = io.BytesIO(encoded.tobytes())
            buf.name = f"thumb_{streamed.name}.jpg"
            return buf, buf.name
        finally:
            cap.release()
    except Exception as e:
        logger.warning(f"生成缩略图失败: {e}")
        return None
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass
