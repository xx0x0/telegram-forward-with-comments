"""
受限频道（开了"禁止转发"）的常规转发：把媒体下到内存再发出去。

相比旧版的关键改动：
- 不再写项目目录的 downloads/，全部走 BytesIO；超过 50MB 才回落到系统 temp（用完即删）
- 视频缩略图从 BytesIO 取帧，不再依赖项目临时目录
- 所有 user-API 写操作过 RateLimiter + safe_call，FloodWait 自愈
"""
import os
from telethon.tl.types import (
    DocumentAttributeVideo, DocumentAttributeFilename,
    InputMediaUploadedPhoto, InputMediaUploadedDocument,
    MessageMediaDocument,
)

from utils import setup_logger, RateLimiter, safe_call, stream_media, make_thumbnail


_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_EXT_TO_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
    ".pdf": "application/pdf", ".zip": "application/zip",
}


class RestrictedNormalForwardHandler:
    def __init__(self, user_client, database, rate_limiter=None):
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        self.rate_limiter = rate_limiter or RateLimiter()

    async def forward_message(self, message, source_channel_id, target_channel_id):
        """转发单条受限消息。"""
        try:
            caption = getattr(message, "message", "") or ""
            target_entity = await self._resolve_entity(target_channel_id)
            if target_entity is None:
                return False

            async with await stream_media(self.user_client, message) as streamed:
                return await self._send_one(streamed, message, target_entity, caption)
        except Exception as e:
            self.logger.error(f"受限单条转发失败: {e}")
            return False

    async def forward_message_group(self, messages, source_channel_id, target_channel_id):
        if not messages:
            return False
        try:
            target_entity = await self._resolve_entity(target_channel_id)
            if target_entity is None:
                return False

            caption = self._best_caption(messages)
            streamed_items = []
            try:
                for msg in messages:
                    if not getattr(msg, "media", None):
                        continue
                    streamed = await stream_media(self.user_client, msg)
                    streamed_items.append((msg, streamed))
                if not streamed_items:
                    return False
                return await self._send_group(streamed_items, target_entity, caption)
            finally:
                for _, s in streamed_items:
                    await s.__aexit__(None, None, None)
        except Exception as e:
            self.logger.error(f"受限媒体组转发失败: {e}")
            return False

    async def _resolve_entity(self, target):
        try:
            if isinstance(target, (int, str)):
                return await safe_call(lambda: self.user_client.get_entity(int(target)))
            return target
        except Exception as e:
            self.logger.error(f"解析目标实体失败: {e}")
            return None

    @staticmethod
    def _best_caption(messages):
        captions = [m.message for m in messages if getattr(m, "message", None)]
        return max(captions, key=len) if captions else ""

    async def _send_one(self, streamed, original_message, target_entity, caption):
        ext = os.path.splitext(streamed.name)[1].lower()

        if ext in _IMAGE_EXTS:
            await safe_call(
                lambda: self.user_client.send_file(
                    entity=target_entity, file=streamed.file, caption=caption,
                ),
                rate_limiter=self.rate_limiter,
            )
            return True

        attributes = []
        thumb_buf = None
        if ext in _VIDEO_EXTS and isinstance(original_message.media, MessageMediaDocument):
            for attr in original_message.media.document.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    attributes.append(DocumentAttributeVideo(
                        duration=attr.duration, w=attr.w, h=attr.h, supports_streaming=True
                    ))
                    break
            thumb_result = make_thumbnail(streamed)
            if thumb_result is not None:
                thumb_buf, _ = thumb_result

        attributes.append(DocumentAttributeFilename(streamed.name))

        try:
            streamed.file.seek(0)
        except Exception:
            pass

        await safe_call(
            lambda: self.user_client.send_file(
                entity=target_entity,
                file=streamed.file,
                thumb=thumb_buf,
                caption=caption,
                attributes=attributes,
            ),
            rate_limiter=self.rate_limiter,
        )
        return True

    async def _send_group(self, streamed_items, target_entity, caption):
        media_inputs = []
        for original_msg, streamed in streamed_items:
            ext = os.path.splitext(streamed.name)[1].lower()
            try:
                streamed.file.seek(0)
            except Exception:
                pass

            uploaded = await safe_call(
                lambda f=streamed.file, n=streamed.name: self.user_client.upload_file(
                    file=f, file_name=n
                ),
                rate_limiter=self.rate_limiter,
            )

            if ext in _IMAGE_EXTS:
                media_inputs.append(InputMediaUploadedPhoto(file=uploaded))
                continue

            attributes = []
            thumb_uploaded = None
            if ext in _VIDEO_EXTS and isinstance(original_msg.media, MessageMediaDocument):
                for attr in original_msg.media.document.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        attributes.append(DocumentAttributeVideo(
                            duration=attr.duration, w=attr.w, h=attr.h, supports_streaming=True
                        ))
                        break
                thumb_result = make_thumbnail(streamed)
                if thumb_result is not None:
                    thumb_buf, thumb_name = thumb_result
                    thumb_uploaded = await safe_call(
                        lambda f=thumb_buf, n=thumb_name: self.user_client.upload_file(file=f, file_name=n),
                        rate_limiter=self.rate_limiter,
                    )

            attributes.append(DocumentAttributeFilename(streamed.name))
            media_inputs.append(InputMediaUploadedDocument(
                file=uploaded,
                thumb=thumb_uploaded,
                mime_type=_EXT_TO_MIME.get(ext, "application/octet-stream"),
                attributes=attributes,
            ))

        await safe_call(
            lambda: self.user_client.send_file(
                entity=target_entity, file=media_inputs, caption=caption,
            ),
            rate_limiter=self.rate_limiter,
        )
        return True


