"""
数据存储模块 - 使用 SQLite 存储招标信息

优化说明（v1.1.1）：
- 使用线程本地存储复用数据库连接，提升性能
- 所有公开方法签名保持不变，完全向后兼容
"""
import sqlite3
import hashlib
import os
import threading
from pathlib import Path
from contextlib import closing
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class BidInfo:
    """招标信息数据类"""
    title: str
    url: str
    publish_date: str
    source: str
    content: str = ""
    purchaser: str = ""
    created_at: str = ""  # SQLite 入库时间，UTC；不是来源发布日期。
    
    @property
    def unique_id(self) -> str:
        """生成唯一标识（基于URL的MD5）"""
        return hashlib.md5(self.url.encode()).hexdigest()


def default_db_path():
    root = Path(__file__).resolve().parents[2]
    override = os.environ.get('BIDMONITOR_DB_PATH')
    if override:
        path = Path(override).expanduser()
        return str((path if path.is_absolute() else root / path).resolve())
    candidates = [root / 'server/data/bids.db', root / 'data/bids.db']
    existing = [p for p in candidates if p.is_file()]
    if len(existing) > 1:
        raise RuntimeError('发现两个历史数据库，请备份后设置 BIDMONITOR_DB_PATH 指定使用哪个；未自动覆盖或合并')
    return str((existing[0] if existing else candidates[0]).resolve())


class Storage:
    """SQLite 数据存储类
    
    使用线程本地存储管理数据库连接，每个线程复用同一个连接，
    避免频繁创建和关闭连接带来的性能开销。
    """
    
    def __init__(self, db_path: str = None):
        db_path = str(Path(db_path).resolve()) if db_path else default_db_path()
        self.db_path = db_path
        # 线程本地存储，用于复用数据库连接
        self._local = threading.local()
        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:  # 处理相对路径情况
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（复用机制）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._local.conn
    
    def close(self):
        """关闭当前线程的数据库连接（用于清理资源）"""
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            try:
                self._local.conn.close()
            except:
                pass
            self._local.conn = None
    
    def _init_db(self):
        """初始化数据库表"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    publish_date TEXT,
                    source TEXT,
                    content TEXT,
                    purchaser TEXT,
                    notified INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_unique_id ON bids(unique_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_notified ON bids(notified)
            """)
            conn.commit()
    
    def exists(self, bid: BidInfo) -> bool:
        """检查招标信息是否已存在"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM bids WHERE unique_id = ?",
            (bid.unique_id,)
        )
        return cursor.fetchone() is not None
    
    def save(self, bid: BidInfo, notified: bool = False) -> bool:
        """保存招标信息，返回是否成功（新记录为True，重复为False）"""
        if self.exists(bid):
            return False
        
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bids (unique_id, title, url, publish_date, source, content, purchaser, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bid.unique_id,
            bid.title,
            bid.url,
            bid.publish_date,
            bid.source,
            bid.content,
            bid.purchaser,
            1 if notified else 0
        ))
        conn.commit()
        return True
    
    def mark_notified(self, bids):
        """标记招标信息已发送通知
        
        Args:
            bids: 可以是单个BidInfo、BidInfo列表、或URL列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 处理不同输入类型
        if isinstance(bids, BidInfo):
            # 单个BidInfo对象
            cursor.execute(
                "UPDATE bids SET notified = 1 WHERE unique_id = ?",
                (bids.unique_id,)
            )
        elif isinstance(bids, list) and len(bids) > 0:
            if isinstance(bids[0], BidInfo):
                # BidInfo列表
                for bid in bids:
                    cursor.execute(
                        "UPDATE bids SET notified = 1 WHERE unique_id = ?",
                        (bid.unique_id,)
                    )
            elif isinstance(bids[0], str):
                # URL列表
                for url in bids:
                    unique_id = hashlib.md5(url.encode()).hexdigest()
                    cursor.execute(
                        "UPDATE bids SET notified = 1 WHERE unique_id = ?",
                        (unique_id,)
                    )
        
        conn.commit()
    
    def get_unnotified(self) -> List[BidInfo]:
        """获取未通知的招标信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, url, publish_date, source, content, purchaser, created_at
            FROM bids WHERE notified = 0
        """)
        rows = cursor.fetchall()
        return [
            BidInfo(
                title=row[0],
                url=row[1],
                publish_date=row[2],
                source=row[3],
                content=row[4],
                purchaser=row[5],
                created_at=row[6]
            )
            for row in rows
        ]
    
    def get_recent(self, days: int = 7) -> List[BidInfo]:
        """获取最近几天的招标信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, url, publish_date, source, content, purchaser, created_at
            FROM bids 
            WHERE datetime(created_at) > datetime('now', ?)
            ORDER BY created_at DESC
        """, (f'-{days} days',))
        rows = cursor.fetchall()
        return [
            BidInfo(
                title=row[0],
                url=row[1],
                publish_date=row[2],
                source=row[3],
                content=row[4],
                purchaser=row[5],
                created_at=row[6]
            )
            for row in rows
        ]
    
    def get_all(self) -> List[BidInfo]:
        """获取所有招标信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, url, publish_date, source, content, purchaser, created_at
            FROM bids 
            ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()
        return [
            BidInfo(
                title=row[0],
                url=row[1],
                publish_date=row[2],
                source=row[3],
                content=row[4],
                purchaser=row[5],
                created_at=row[6]
            )
            for row in rows
        ]
    
    def count_all(self) -> int:
        """获取总记录数"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bids")
        return cursor.fetchone()[0]

    def clear_all(self, scope='bids'):
        """Clear an explicit scope atomically. Default preserves legacy GUI semantics."""
        if scope not in ('bids', 'competitors', 'all'):
            raise ValueError('无效清理范围')
        conn = self._get_connection()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with conn:
            if scope in ('bids', 'all'):
                conn.execute('DELETE FROM bids')
            if scope in ('competitors', 'all'):
                for table in ('competitor_observations', 'competitor_records'):
                    if table in tables:
                        conn.execute('DELETE FROM ' + table)
