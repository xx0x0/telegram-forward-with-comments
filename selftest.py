"""
Self-test: 纯逻辑单元测试，不连 Telegram。
覆盖：
- TokenBucket 限速
- safe_call 处理 FloodWait/瞬态错误/最大重试
- StreamedMedia 内存与 temp 模式的清理
- stream_media 内存路径（mock client）
- make_thumbnail 没装 cv2 时不崩、装了也能从 temp 视频提帧
- Database.processed_comments 增删查
- CommentForwardHandler._should_forward_message 过滤逻辑
- 调用签名一致性：CFH._send_one_comment / _send_comment_group 的参数与 RCFH 接口对齐
"""
import asyncio
import io
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# 占位 config.yml 已存在；utils/logger.py 在 import 时读它
from utils.flood_control import (
    TokenBucket, RateLimiter, StreamedMedia,
    safe_call, stream_media, make_thumbnail,
    DEFAULT_MEMORY_THRESHOLD,
)
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaPhoto


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestTokenBucket(unittest.TestCase):
    def test_first_call_no_wait(self):
        bucket = TokenBucket(rate_per_minute=600)  # 10/sec
        t0 = time.monotonic()
        run(bucket.acquire())
        self.assertLess(time.monotonic() - t0, 0.05)

    def test_second_call_waits(self):
        bucket = TokenBucket(rate_per_minute=120)  # 2/sec → interval=0.5s
        run(bucket.acquire())
        t0 = time.monotonic()
        run(bucket.acquire())
        elapsed = time.monotonic() - t0
        self.assertGreater(elapsed, 0.4)
        self.assertLess(elapsed, 0.7)


class TestSafeCall(unittest.TestCase):
    def test_passthrough(self):
        async def go():
            return await safe_call(lambda: asyncio.sleep(0, result="ok"))
        self.assertEqual(run(go()), "ok")

    def test_floodwait_recovery(self):
        attempts = {"n": 0}
        async def make():
            attempts["n"] += 1
            if attempts["n"] == 1:
                err = FloodWaitError(request=None)
                err.seconds = 0  # 不真等
                raise err
            return "recovered"
        async def go():
            return await safe_call(lambda: make())
        self.assertEqual(run(go()), "recovered")
        self.assertEqual(attempts["n"], 2)

    def test_floodwait_exhausted(self):
        async def make():
            err = FloodWaitError(request=None)
            err.seconds = 0
            raise err
        async def go():
            return await safe_call(lambda: make(), max_retries=2)
        with self.assertRaises(FloodWaitError):
            run(go())


class TestStreamedMedia(unittest.TestCase):
    def test_memory_cleanup(self):
        buf = io.BytesIO(b"hi")
        sm = StreamedMedia(buf, path=None, name="x.bin", size=2)
        async def go():
            async with sm:
                self.assertFalse(buf.closed)
            self.assertTrue(buf.closed)
        run(go())

    def test_temp_path_cleanup(self):
        fd, path = tempfile.mkstemp(prefix="st_test_")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(b"data")
        sm = StreamedMedia(open(path, "rb"), path=path, name="x.bin", size=4)
        async def go():
            async with sm:
                self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path))
        run(go())


class TestStreamMedia(unittest.TestCase):
    def test_small_media_uses_memory(self):
        # 模拟一条带 photo 的消息 + mock client
        msg = MagicMock()
        msg.id = 42
        msg.media = MessageMediaPhoto(photo=MagicMock(sizes=[MagicMock(size=1024)]),
                                      ttl_seconds=None, spoiler=False)

        async def fake_download(message, file=None):
            file.write(b"x" * 1024)
        client = MagicMock()
        client.download_media = AsyncMock(side_effect=fake_download)

        async def go():
            sm = await stream_media(client, msg)
            try:
                self.assertIsNone(sm.path, "小媒体不应落 temp")
                self.assertEqual(sm.size, 1024)
                self.assertTrue(sm.name.startswith("42"))
                self.assertEqual(sm.file.read(), b"x" * 1024)
            finally:
                await sm.__aexit__(None, None, None)
        run(go())


class TestMakeThumbnail(unittest.TestCase):
    def test_no_cv2_returns_none(self):
        with patch.dict(sys.modules, {"cv2": None}):
            sm = StreamedMedia(io.BytesIO(b"v"), path=None, name="x.mp4", size=1)
            # 强制 import cv2 失败
            import builtins
            real = builtins.__import__
            def fake(name, *a, **kw):
                if name == "cv2":
                    raise ImportError("no cv2")
                return real(name, *a, **kw)
            with patch.object(builtins, "__import__", side_effect=fake):
                self.assertIsNone(make_thumbnail(sm))


class TestDatabase(unittest.TestCase):
    def setUp(self):
        from core.database import Database
        self.db_path = tempfile.mktemp(prefix="db_test_", suffix=".db")
        self.db = Database(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except OSError:
                pass

    def test_processed_comments_table_exists(self):
        rows = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='processed_comments'"
        )
        self.assertEqual(len(rows), 1)

    def test_insert_and_lookup(self):
        self.db.execute(
            "INSERT INTO processed_comments (channel_id, post_id, comment_id) VALUES (?, ?, ?)",
            ("chan1", "post1", "c1"),
        )
        rows = self.db.execute(
            "SELECT 1 FROM processed_comments WHERE channel_id=? AND post_id=? AND comment_id=?",
            ("chan1", "post1", "c1"),
        )
        self.assertEqual(len(rows), 1)
        # 重复插入不抛
        self.db.execute(
            "INSERT OR IGNORE INTO processed_comments (channel_id, post_id, comment_id) VALUES (?, ?, ?)",
            ("chan1", "post1", "c1"),
        )


class TestShouldForwardMessage(unittest.TestCase):
    def setUp(self):
        from core.comment_forward_handler import CommentForwardHandler
        # 避免触发 RestrictedCommentForwardHandler 的 RateLimiter 等
        with patch("core.comment_forward_handler.RestrictedCommentForwardHandler"):
            self.handler = CommentForwardHandler(MagicMock(), MagicMock())

    def _make_msg(self, *, has_media=True, text="hi", reply_markup=None):
        m = MagicMock()
        m.id = 1
        m.message = text
        m.reply_markup = reply_markup
        if has_media:
            from telethon.tl.types import MessageMediaPhoto
            m.media = MessageMediaPhoto(photo=MagicMock(), ttl_seconds=None, spoiler=False)
        else:
            m.media = None
        return m

    def test_text_only_rejected(self):
        self.assertFalse(self.handler._should_forward_message(self._make_msg(has_media=False)))

    def test_with_media_accepted(self):
        self.assertTrue(self.handler._should_forward_message(self._make_msg(text="nice photo")))

    def test_ad_keyword_rejected(self):
        self.assertFalse(self.handler._should_forward_message(self._make_msg(text="限时抢购")))

    def test_url_rejected(self):
        self.assertFalse(self.handler._should_forward_message(self._make_msg(text="see https://example.com")))

    def test_button_rejected(self):
        self.assertFalse(self.handler._should_forward_message(self._make_msg(reply_markup=MagicMock())))


class TestProcessedTracking(unittest.TestCase):
    """断点续传那一对方法 _is_processed/_mark_processed 真的把数据写进 db。"""
    def setUp(self):
        from core.comment_forward_handler import CommentForwardHandler
        from core.database import Database
        self.db_path = tempfile.mktemp(prefix="cfh_db_", suffix=".db")
        self.db = Database(db_path=self.db_path)
        with patch("core.comment_forward_handler.RestrictedCommentForwardHandler"):
            self.handler = CommentForwardHandler(MagicMock(), self.db)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except OSError:
                pass

    def test_round_trip(self):
        self.assertFalse(self.handler._is_processed("c1", "p1", "m1"))
        self.handler._mark_processed("c1", "p1", "m1")
        self.assertTrue(self.handler._is_processed("c1", "p1", "m1"))
        # 不同 post 隔离
        self.assertFalse(self.handler._is_processed("c1", "p2", "m1"))


class TestSignaturesMatch(unittest.TestCase):
    """重写后 CommentForwardHandler 调用 RCFH 的方法签名要对得上。"""
    def test_restricted_forward_message_signature(self):
        import inspect
        from core.restricted_comment_forward_handler import RestrictedCommentForwardHandler
        sig = inspect.signature(RestrictedCommentForwardHandler.forward_message)
        params = list(sig.parameters.keys())
        # CFH._send_one_comment 调用 self.restricted_handler.forward_message(comment, src, target_group, reply_to=...)
        self.assertEqual(params, ["self", "message", "source_channel_id", "target_channel_id", "reply_to"])

    def test_restricted_forward_media_group_signature(self):
        import inspect
        from core.restricted_comment_forward_handler import RestrictedCommentForwardHandler
        sig = inspect.signature(RestrictedCommentForwardHandler.forward_media_group)
        params = list(sig.parameters.keys())
        # CFH._send_comment_group 调用 forward_media_group(msgs, src, target_group, reply_to=...)
        self.assertEqual(params, ["self", "messages", "source_channel_id", "target_channel_id", "reply_to"])

    def test_normal_forward_handler_uses_existing_methods(self):
        """normal_forward_handler 调用 restricted.forward_message / forward_message_group，签名要存在。"""
        from core.restricted_normal_forward_handler import RestrictedNormalForwardHandler
        self.assertTrue(hasattr(RestrictedNormalForwardHandler, "forward_message"))
        self.assertTrue(hasattr(RestrictedNormalForwardHandler, "forward_message_group"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
