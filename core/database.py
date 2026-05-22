import sqlite3
from utils import setup_logger


class Database:
    def __init__(self, db_path="data.db"):
        """
        初始化数据库对象，并建立连接及初始化表结构。

        参数:
            db_path: 数据库文件路径，默认使用 data.db
        """
        self.db_path = db_path
        self.logger = setup_logger(__name__)
        self.create_tables()
        self.create_indexes()

    def execute(self, query, params=()):
        """执行SQL查询语句"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            result = cursor.fetchall()
            cursor.close()
            conn.close()
            return result
        except Exception as e:
            self.logger.error(f"执行SQL出错: {str(e)}")
            raise

    def create_tables(self):
        """创建数据库表"""
        # 创建目标频道表  is_transfer 0 不转发 1 转发
        query_target_channels = """
        CREATE TABLE IF NOT EXISTS target_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_id)
        );
        """
        self.execute(query_target_channels)

        # 创建资源频道表  send_type 0 正常发送 1 评论发送 
        # 是否转发 0 不转发 1 转发
        query_resource_channels = """
        CREATE TABLE IF NOT EXISTS resource_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            target_channel_id TEXT DEFAULT NULL,
            is_transfer INTEGER DEFAULT 0,
            send_type INTEGER DEFAULT 0,
            send_interval FLOAT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_id)
        );
        """
        self.execute(query_resource_channels)

        # 创建资源消息表
        query_resource_messages = """
        CREATE TABLE IF NOT EXISTS resource_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        self.execute(query_resource_messages)

        # 评论级别的断点续传表：哪个 (channel_id, post_id) 下的哪条评论已经处理过
        # 用于 100+ 评论中途崩溃后恢复，不会重复转发已发过的评论
        query_processed_comments = """
        CREATE TABLE IF NOT EXISTS processed_comments (
            channel_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (channel_id, post_id, comment_id)
        );
        """
        self.execute(query_processed_comments)

    def create_indexes(self):
        """创建数据库索引"""
        # 创建目标频道索引
        self.execute("CREATE INDEX IF NOT EXISTS idx_target_channels_channel_id ON target_channels(channel_id);")
        
        # 创建资源频道索引
        self.execute("CREATE INDEX IF NOT EXISTS idx_resource_channels_channel_id ON resource_channels(channel_id);")

        # 创建资源消息索引
        self.execute("CREATE INDEX IF NOT EXISTS idx_resource_messages_channel_id ON resource_messages(channel_id);")

        # processed_comments 主键已自带 (channel_id, post_id, comment_id) 复合索引
        self.execute("CREATE INDEX IF NOT EXISTS idx_processed_comments_post ON processed_comments(channel_id, post_id);")

   