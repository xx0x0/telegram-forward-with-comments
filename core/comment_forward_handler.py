"""
评论转发：把源频道的帖子 + 帖子下的评论一起搬到目标频道的关联讨论群组。

相比旧版的关键改动：
- 全局 RateLimiter 控发送节奏，避免主动撞 Telegram 风控
- 所有 user-API 写操作走 safe_call，自动处理 FloodWait
- 评论按"先媒体组后单条 + 时间正序"排序，不再错位
- 每条评论的处理结果落 db（processed_comments 表），中途崩溃可断点续传
- 临时事件监听器走 try/finally，不再泄漏
- 媒体不落项目目录，BytesIO 中转（>50MB 才回落系统 temp）
"""
from telethon import events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest

import asyncio
import re
import traceback

from utils import setup_logger, RateLimiter, safe_call
from core.restricted_comment_forward_handler import RestrictedCommentForwardHandler


COMMENT_RATE_PER_MINUTE = 18
COMMENT_CONCURRENCY = 1

class CommentForwardHandler:
    def __init__(self, user_client, database):
        self.user_client = user_client
        self.database = database
        self.logger = setup_logger(__name__)
        self.rate_limiter = RateLimiter(COMMENT_RATE_PER_MINUTE, COMMENT_CONCURRENCY)
        self.restricted_handler = RestrictedCommentForwardHandler(
            user_client, database, rate_limiter=self.rate_limiter
        )
        self.synced_messages = {}

        self.ad_keywords = [
            "推广", "广告", "促销", "优惠", "折扣", "抢购", "限时", "活动",
            "官网", "链接", "联系", "咨询", "电话", "微信", "加群", "加入",
            "ad", "sponsor", "promotion", "discount", "offer", "sale",
            "buy", "price", "contact", "join", "group", "channel", "vip",
        ]
        self.url_pattern = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

    # ----- 工具方法 -----
    def _has_media(self, message):
        return getattr(message, "media", None) and (
            isinstance(message.media, MessageMediaPhoto)
            or isinstance(message.media, MessageMediaDocument)
        )

    def _should_forward_message(self, message):
        if not self._has_media(message):
            return False
        if getattr(message, "reply_markup", None):
            return False
        text = (message.message or "").lower() if getattr(message, "message", None) else ""
        if text:
            for kw in self.ad_keywords:
                if kw.lower() in text:
                    return False
            if self.url_pattern.search(text):
                return False
        return True

    def _is_processed(self, channel_id, post_id, comment_id) -> bool:
        rows = self.database.execute(
            "SELECT 1 FROM processed_comments WHERE channel_id=? AND post_id=? AND comment_id=? LIMIT 1",
            (str(channel_id), str(post_id), str(comment_id)),
        )
        return bool(rows)

    def _mark_processed(self, channel_id, post_id, comment_id):
        self.database.execute(
            "INSERT OR IGNORE INTO processed_comments (channel_id, post_id, comment_id) VALUES (?, ?, ?)",
            (str(channel_id), str(post_id), str(comment_id)),
        )

    # ----- 主流程入口 -----
    async def forward_messages(self, source_channel_id, provided_messages=None):
        """从源频道挑一条新帖子转发到目标频道，并把帖子下评论同步到讨论群组。"""
        try:
            source_info = self.database.execute(
                """SELECT channel_id, channel_name, target_channel_id
                   FROM resource_channels WHERE channel_id = ? AND is_transfer = 1""",
                (source_channel_id,),
            )
            if not source_info:
                self.logger.error(f"未找到频道ID为 {source_channel_id} 的可转发频道")
                return False
            source_channel_id, source_name, target_channel_id = source_info[0]
            if not target_channel_id:
                self.logger.error(f"频道 {source_name} 未设置目标频道")
                return False

            target_full = await safe_call(
                lambda: self.user_client(GetFullChannelRequest(channel=int(target_channel_id)))
            )
            linked_group_id = target_full.full_chat.linked_chat_id
            if not linked_group_id:
                self.logger.error(f"目标频道 {target_channel_id} 没有关联讨论群组")
                return False

            forwarded_rows = self.database.execute(
                "SELECT message_id FROM resource_messages WHERE channel_id = ?",
                (source_channel_id,),
            )
            forwarded_ids = {r[0] for r in (forwarded_rows or [])}
            self.logger.info(f"已有 {len(forwarded_ids)} 条消息被标记为已处理")

            if provided_messages:
                new_messages = [m for m in provided_messages if str(m.id) not in forwarded_ids]
                if not new_messages:
                    return True
                return await self._process_messages_batch(
                    new_messages, source_channel_id, target_channel_id, linked_group_id, forwarded_ids
                )

            # 自动模式：分批向前找最新一条可转发的
            last_id = 0
            while True:
                params = {"entity": int(source_channel_id), "limit": 30}
                if last_id > 0:
                    params["max_id"] = last_id
                messages = await safe_call(lambda: self.user_client.get_messages(**params))
                if not messages:
                    return False
                last_id = min(m.id for m in messages)
                new_messages = [m for m in messages if str(m.id) not in forwarded_ids]
                if not new_messages:
                    await asyncio.sleep(2)
                    continue
                ok = await self._process_messages_batch(
                    new_messages, source_channel_id, target_channel_id, linked_group_id, forwarded_ids
                )
                if ok:
                    return True
                await asyncio.sleep(2)
        except Exception as e:
            self.logger.error(f"评论转发出错: {e}")
            self.logger.error(traceback.format_exc())
            return False

    # ----- 处理一批消息：挑出符合条件的最新一条（含媒体组），转发主帖 + 评论 -----
    async def _process_messages_batch(self, new_messages, source_channel_id,
                                      target_channel_id, linked_group_id, forwarded_ids):
        filtered = []
        for msg in new_messages:
            if self._should_forward_message(msg):
                filtered.append(msg)
            else:
                self._mark_msg_processed(source_channel_id, msg.id)
        if not filtered:
            return False

        latest = filtered[0]

        if latest.grouped_id:
            # 收集同组消息（含尝试补全）
            current_gid = latest.grouped_id
            grouped = [m for m in filtered if m.grouped_id == current_gid]
            msg_ids = [m.id for m in grouped]
            min_id, max_id = min(msg_ids), max(msg_ids)
            if max_id - min_id + 1 > len(grouped):
                try:
                    extra = await safe_call(lambda: self.user_client.get_messages(
                        entity=int(source_channel_id), min_id=min_id - 1, max_id=max_id + 1
                    ))
                    for m in extra:
                        if m.grouped_id == current_gid and m.id not in msg_ids and str(m.id) not in forwarded_ids:
                            if self._should_forward_message(m):
                                grouped.append(m)
                                msg_ids.append(m.id)
                            else:
                                self._mark_msg_processed(source_channel_id, m.id)
                except Exception as e:
                    self.logger.warning(f"补全媒体组失败: {e}")

            # 整组中只要有一条不合格，整组跳过
            if any(not self._should_forward_message(m) for m in grouped):
                for m in grouped:
                    self._mark_msg_processed(source_channel_id, m.id)
                return False

            grouped.sort(key=lambda m: m.id)
            forwarded = await self._forward_media_group(grouped, target_channel_id)
            if not forwarded:
                return False

            synced = await self._wait_for_sync(forwarded, target_channel_id, linked_group_id, grouped[0])
            if synced:
                await self._forward_comment_media(grouped[0], source_channel_id, linked_group_id, synced)
            for m in grouped:
                self._mark_msg_processed(source_channel_id, m.id)
            return True

        forwarded = await self._forward_main_media(latest, target_channel_id)
        if not forwarded:
            return False
        synced = await self._wait_for_sync(forwarded, target_channel_id, linked_group_id, latest)
        if synced:
            await self._forward_comment_media(latest, source_channel_id, linked_group_id, synced)
        self._mark_msg_processed(source_channel_id, latest.id)
        return True

    def _mark_msg_processed(self, channel_id, msg_id):
        self.database.execute(
            "INSERT INTO resource_messages (channel_id, message_id) VALUES (?, ?)",
            (str(channel_id), str(msg_id)),
        )

    # ----- 主媒体转发（单条 / 媒体组），带 fallback 到受限处理器 -----
    async def _forward_main_media(self, message, target_channel_id):
        try:
            forwarded = await safe_call(
                lambda: self.user_client.forward_messages(
                    entity=int(target_channel_id), messages=message, drop_author=True
                ),
                rate_limiter=self.rate_limiter,
            )
            if forwarded:
                return forwarded
        except Exception as e:
            self.logger.warning(f"主帖直接转发失败 ({message.id})：{e}，切到受限模式")

        src_channel_id = self._extract_source_channel(message)
        ok = await self.restricted_handler.forward_message(message, src_channel_id, target_channel_id)
        if ok:
            class _Stub:
                def __init__(self, mid): self.id = mid
            return _Stub(message.id)
        return None

    async def _forward_media_group(self, messages, target_channel_id):
        if not messages:
            return None
        sorted_msgs = sorted(messages, key=lambda m: m.id)
        try:
            forwarded = await safe_call(
                lambda: self.user_client.forward_messages(
                    entity=int(target_channel_id), messages=sorted_msgs, drop_author=True
                ),
                rate_limiter=self.rate_limiter,
            )
            if forwarded:
                return forwarded
        except Exception as e:
            self.logger.warning(f"媒体组直接转发失败：{e}，切到受限模式")

        src_channel_id = self._extract_source_channel(sorted_msgs[0])
        ok = await self.restricted_handler.forward_media_group(
            sorted_msgs, src_channel_id, target_channel_id
        )
        if not ok:
            return None
        class _Stub:
            def __init__(self, mid): self.id = mid
        return [_Stub(m.id) for m in sorted_msgs]

    @staticmethod
    def _extract_source_channel(message):
        peer = getattr(message, "peer_id", None)
        if peer is not None and hasattr(peer, "channel_id"):
            return peer.channel_id
        return getattr(message, "chat_id", None)

    # ----- 等待主帖同步到讨论群组（用于评论挂载点） -----
    async def _wait_for_sync(self, forwarded_message, channel_id, group_id, original_message):
        try:
            if isinstance(forwarded_message, list):
                forwarded_ids = [m.id for m in forwarded_message]
                msg_id_to_use = min(forwarded_ids)
            else:
                forwarded_ids = None
                msg_id_to_use = forwarded_message.id

            try:
                discussion = await safe_call(lambda: self.user_client(GetDiscussionMessageRequest(
                    peer=int(channel_id), msg_id=msg_id_to_use
                )))
                if discussion and discussion.messages:
                    synced = discussion.messages[0]
                    if forwarded_ids is None:
                        return synced
                    # 媒体组：按 id 偏移推算每条同步消息
                    offset = synced.id - min(forwarded_ids)
                    class _Stub:
                        def __init__(self, mid): self.id = mid
                    return [_Stub(orig + offset) for orig in forwarded_ids]
            except Exception as e:
                self.logger.warning(f"GetDiscussionMessage 失败：{e}，回退到事件监听")

            # 回退方案：监听讨论群新消息
            wait_key = f"{channel_id}_{msg_id_to_use}"
            self.synced_messages[wait_key] = None
            handler = None

            async def _on_new(event):
                if not self._has_media(event.message):
                    return
                self.synced_messages[wait_key] = event.message

            try:
                handler = self.user_client.add_event_handler(
                    _on_new, events.NewMessage(chats=int(group_id))
                )
                for _ in range(15):
                    if self.synced_messages.get(wait_key):
                        return self.synced_messages[wait_key]
                    await asyncio.sleep(1)
                self.logger.warning("等待消息同步超时")
                return None
            finally:
                try:
                    if handler is not None:
                        self.user_client.remove_event_handler(_on_new)
                except Exception:
                    pass
                self.synced_messages.pop(wait_key, None)
        except Exception as e:
            self.logger.error(f"等待消息同步出错: {e}")
            return None

    @staticmethod
    def _synced_message_id(synced):
        if isinstance(synced, list):
            return synced[0].id
        return synced.id

    # ----- 把评论同步到讨论群组（这里是 100+ 评论崩的地方，重写） -----
    async def _forward_comment_media(self, original_message, source_channel_id, group_id, synced_message):
        """
        关键改动：
        1. 先按时间正序拉所有评论，分组：先媒体组、再单条；不再边拉边发导致顺序错乱
        2. 每条/每组发送都过 RateLimiter（限速 + 信号量），过 safe_call（FloodWait 自愈）
        3. 每条发送成功后写 processed_comments，断点续传：下次跑直接跳过已发的
        4. 直接发失败 → 走 restricted_handler，仍然在限速器里
        """
        try:
            try:
                target_group = await safe_call(lambda: self.user_client.get_entity(int(group_id)))
            except Exception as e:
                self.logger.error(f"获取讨论群实体失败: {e}")
                return

            comments = []
            async for c in self.user_client.iter_messages(
                entity=int(source_channel_id),
                reply_to=original_message.id,
                limit=None,
                reverse=True,  # 旧的在前，按时间正序
            ):
                if c is not None:
                    comments.append(c)
            self.logger.info(f"获取评论 {len(comments)} 条")

            media_comments = [c for c in comments if self._has_media(c)]
            if not media_comments:
                return

            # 过滤已处理过的（断点续传）
            post_id = original_message.id
            pending = []
            for c in media_comments:
                if self._is_processed(source_channel_id, post_id, c.id):
                    continue
                pending.append(c)
            if not pending:
                self.logger.info("所有评论都已处理过，跳过")
                return
            self.logger.info(f"待处理评论：{len(pending)}/{len(media_comments)}（其余已断点续传跳过）")

            # 拆分成"媒体组"和"单条"，分别按 id 排序
            singles = [c for c in pending if not c.grouped_id]
            groups: dict = {}
            for c in pending:
                if c.grouped_id:
                    groups.setdefault(c.grouped_id, []).append(c)

            synced_id = self._synced_message_id(synced_message)
            sent_count = 0
            failed_count = 0

            # 先发单条评论，后发媒体组评论，每条都串行
            for c in sorted(singles, key=lambda m: m.id):
                ok = await self._send_one_comment(c, source_channel_id, target_group, synced_id)
                if ok:
                    self._mark_processed(source_channel_id, post_id, c.id)
                    sent_count += 1
                else:
                    failed_count += 1

            for gid, msgs in groups.items():
                msgs = await self._maybe_complete_group(msgs, source_channel_id, original_message.id)
                msgs.sort(key=lambda m: m.id)
                ok = await self._send_comment_group(msgs, source_channel_id, target_group, synced_id)
                if ok:
                    for m in msgs:
                        self._mark_processed(source_channel_id, post_id, m.id)
                    sent_count += len(msgs)
                else:
                    failed_count += len(msgs)

            self.logger.info(f"评论转发完成：成功 {sent_count}，失败 {failed_count}，总计 {len(media_comments)}")
        except Exception as e:
            self.logger.error(f"获取评论出错: {e}")
            self.logger.error(traceback.format_exc())

    async def _maybe_complete_group(self, msgs, source_channel_id, post_id):
        """媒体组评论可能拉漏，尝试在原 ID 范围内补全。"""
        ids = [m.id for m in msgs]
        if not ids:
            return msgs
        min_id, max_id = min(ids), max(ids)
        if max_id - min_id + 1 <= len(msgs):
            return msgs
        gid = msgs[0].grouped_id
        try:
            extra = await safe_call(lambda: self.user_client.get_messages(
                entity=int(source_channel_id),
                min_id=min_id - 1, max_id=max_id + 1,
                reply_to=post_id,
            ))
            for m in extra:
                if m.grouped_id == gid and m.id not in ids:
                    msgs.append(m)
                    ids.append(m.id)
        except Exception as e:
            self.logger.warning(f"补全评论媒体组失败: {e}")
        return msgs

    async def _send_one_comment(self, comment, source_channel_id, target_group, reply_to_id):
        """发单条评论：先尝试直发，失败回落到受限处理器。"""
        try:
            await safe_call(
                lambda: self.user_client.send_message(
                    entity=target_group,
                    message=comment.message or "",
                    file=comment.media,
                    reply_to=reply_to_id,
                ),
                rate_limiter=self.rate_limiter,
            )
            return True
        except Exception as e:
            self.logger.warning(f"直发评论 {comment.id} 失败：{e}，切受限模式")

        try:
            return await self.restricted_handler.forward_message(
                comment, self._extract_source_channel(comment) or source_channel_id,
                target_group, reply_to=reply_to_id
            )
        except Exception as e:
            self.logger.error(f"评论 {comment.id} 受限发送也失败：{e}")
            return False

    async def _send_comment_group(self, msgs, source_channel_id, target_group, reply_to_id):
        if not msgs:
            return False
        try:
            caption = next((m.message for m in msgs if getattr(m, "message", None)), "")
            await safe_call(
                lambda: self.user_client.send_file(
                    entity=target_group,
                    file=[m.media for m in msgs],
                    caption=caption,
                    reply_to=reply_to_id,
                    album=True,
                ),
                rate_limiter=self.rate_limiter,
            )
            return True
        except Exception as e:
            self.logger.warning(f"直发评论媒体组失败：{e}，切受限模式")

        try:
            return await self.restricted_handler.forward_media_group(
                msgs,
                self._extract_source_channel(msgs[0]) or source_channel_id,
                target_group, reply_to=reply_to_id
            )
        except Exception as e:
            self.logger.error(f"评论媒体组受限发送也失败：{e}")
            return False







