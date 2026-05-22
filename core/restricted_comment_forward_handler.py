"""
受限频道评论转发：用于源频道开了"禁止转发"时，把媒体下到内存再发出去。

相比旧版的关键改动：
- 不再写项目目录的 downloads/，全部走 BytesIO；超过 50MB 才回落到系统 temp（用完即删）
- 视频缩略图也走 BytesIO（cv2 提帧时如需要会借用一次系统 temp，秒级清理）
- 所有 user-API 写操作过 RateLimiter + safe_call，与外层 CommentForwardHandler 共用同一个限速器
"""
import os
from telethon.tl.types import (
    DocumentAttributeVideo, DocumentAttributeFilename,
    InputMediaUploadedPhoto, InputMediaUploadedDocument,
    MessageMediaPhoto, MessageMediaDocument,
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


class RestrictedCommentForwardHandler:
    def __init__(self, user_client, database, rate_limiter=None):
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        # 评论侧的限速器由外层传入，与 CommentForwardHandler 共享配额
        self.rate_limiter = rate_limiter or RateLimiter()

    # ----- 入口：单条评论 -----
    async def forward_message(self, message, source_channel_id, target_channel_id, reply_to=None):
        """转发单条受限评论。target_channel_id 可以是已 resolve 的实体或 id。"""
        try:
            caption = getattr(message, "message", "") or ""
            target_entity = await self._resolve_entity(target_channel_id)
            if target_entity is None:
                return False

            async with await stream_media(self.user_client, message) as streamed:
                return await self._send_one(streamed, message, target_entity, caption, reply_to)
        except Exception as e:
            self.logger.error(f"受限单条评论转发失败: {e}")
            return False

    # ----- 入口：评论媒体组 -----
    async def forward_media_group(self, messages, source_channel_id, target_channel_id, reply_to=None):
        if not messages:
            return False
        try:
            target_entity = await self._resolve_entity(target_channel_id)
            if target_entity is None:
                return False

            caption = self._best_caption(messages)
            streamed_items = []
            try:
                # 先全部下到内存/temp
                for msg in messages:
                    if not getattr(msg, "media", None):
                        continue
                    streamed = await stream_media(self.user_client, msg)
                    streamed_items.append((msg, streamed))
                if not streamed_items:
                    return False
                return await self._send_group(streamed_items, target_entity, caption, reply_to)
            finally:
                # 手动清理（因为没用 async with）
                for _, s in streamed_items:
                    await s.__aexit__(None, None, None)
        except Exception as e:
            self.logger.error(f"受限评论媒体组转发失败: {e}")
            return False

    async def _resolve_entity(self, target):
        """target 可以是 id/str/已 resolve 的 entity。"""
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

    # ----- 真正的发送：单条 -----
    async def _send_one(self, streamed, original_message, target_entity, caption, reply_to):
        """从内存流（或 temp）上传并发送单条媒体。"""
        ext = os.path.splitext(streamed.name)[1].lower()

        # 图片：直接 send_file 走 file=BytesIO
        if ext in _IMAGE_EXTS:
            await safe_call(
                lambda: self.user_client.send_file(
                    entity=target_entity,
                    file=streamed.file,
                    caption=caption,
                    reply_to=reply_to,
                ),
                rate_limiter=self.rate_limiter,
            )
            return True

        # 视频/文档：组装 attributes，必要时附缩略图
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

        # 重新把流定位到开头（make_thumbnail 内部可能动过指针）
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
                reply_to=reply_to,
            ),
            rate_limiter=self.rate_limiter,
        )
        return True

    # ----- 真正的发送：媒体组 -----
    async def _send_group(self, streamed_items, target_entity, caption, reply_to):
        """构造 InputMediaUploadedPhoto/Document 列表后整组发送。"""
        from telethon.utils import get_input_location  # 仅为类型 hint，避免顶层不必要 import

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
                entity=target_entity,
                file=media_inputs,
                caption=caption,
                reply_to=reply_to,
            ),
            rate_limiter=self.rate_limiter,
        )
        return True



