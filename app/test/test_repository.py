#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : test_repository.py
# @Description : 仓储层测试：使用临时 SQLite，不污染真实数据库
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.db.database import Base, create_sqlite_engine
from app.repository import stock_repository as stock_repo
from app.repository.stock_repository import StockRepository, session_scope


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="alphaquant_test_")
        db_path = Path(self.tmp_dir) / "test.db"
        # 用**生产同款**连接配置（WAL + busy timeout）。
        # 直接 create_engine 拿不到 WAL —— 那等于用另一套配置测试，
        # "批量写 + 并发读"的锁问题在测试里永远不会出现。
        self.engine = create_sqlite_engine(f"sqlite:///{db_path.as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _records(self, symbol="000001"):
        return [
            {
                "symbol": symbol,
                "date": "2024-01-02",
                "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
                "volume": 100, "amount": 110.0,
            },
            {
                "symbol": symbol,
                "date": "2024-01-03",
                "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2,
                "volume": 200, "amount": 240.0,
            },
        ]


class TestUpsert(RepositoryTestCase):
    def test_insert(self):
        n = StockRepository.batch_upsert(self.db, self._records())
        self.assertEqual(n, 2)

        df = StockRepository.get_stock_df(self.db, "000001")
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume", "amount"])
        self.assertEqual(df.index.name, "date")

    def test_upsert_updates_existing(self):
        """重复采集应覆盖旧值，而不是被忽略。"""
        StockRepository.batch_upsert(self.db, self._records())
        updated = self._records()
        updated[0]["close"] = 9.99
        StockRepository.batch_upsert(self.db, updated)

        df = StockRepository.get_stock_df(self.db, "000001")
        self.assertAlmostEqual(float(df.iloc[0]["close"]), 9.99)
        # 不能变成 4 行
        self.assertEqual(len(df), 2)

    def test_empty_records(self):
        self.assertEqual(StockRepository.batch_upsert(self.db, []), 0)

    def test_missing_field_raises(self):
        with self.assertRaises(ValueError):
            StockRepository.batch_upsert(self.db, [{"close": 1.0}])


class TestQuery(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        StockRepository.batch_upsert(self.db, self._records())

    def test_date_filter(self):
        df = StockRepository.get_stock_df(
            self.db, "000001", "2024-01-03", "2024-01-03"
        )
        self.assertEqual(len(df), 1)

    def test_symbol_normalization(self):
        df = StockRepository.get_stock_df(self.db, "000001.SZ")
        self.assertEqual(len(df), 2)

    def test_empty_result_schema(self):
        df = StockRepository.get_stock_df(self.db, "999999")
        self.assertTrue(df.empty)
        self.assertIn("close", df.columns)

    def test_list_and_coverage(self):
        items = StockRepository.list_symbols(self.db)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "000001")

        cov = StockRepository.get_coverage(self.db, "000001")
        self.assertEqual(cov["rows"], 2)
        self.assertEqual(cov["start"], "2024-01-02")

        self.assertIsNone(StockRepository.get_coverage(self.db, "999999"))

    def test_has_data(self):
        self.assertTrue(StockRepository.has_data(self.db, "000001"))
        self.assertFalse(StockRepository.has_data(self.db, "999999"))


class TestSessionScope(unittest.TestCase):
    """验证 session_scope 的提交/回滚语义（用临时库替换掉全局 SessionLocal）。"""

    def setUp(self):
        import tempfile
        from pathlib import Path

        tmp_dir = tempfile.mkdtemp(prefix="alphaquant_test_")
        # 同样用生产同款连接配置（WAL），避免"测试用另一套连接配置"
        self.engine = create_sqlite_engine(f"sqlite:///{Path(tmp_dir) / 't.db'}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        self._orig = stock_repo.SessionLocal
        stock_repo.SessionLocal = self.Session

    def tearDown(self):
        stock_repo.SessionLocal = self._orig
        self.engine.dispose()

    def test_commits_on_success(self):
        records = [{
            "symbol": "000001", "date": "2024-01-02",
            "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
            "volume": 100, "amount": 110.0,
        }]
        with session_scope() as db:
            StockRepository.batch_upsert(db, records)

        with session_scope() as db:
            self.assertEqual(len(StockRepository.get_stock_df(db, "000001")), 1)

    def test_rolls_back_on_error(self):
        """commit=False 时事务交由 session_scope 控制，异常应整体回滚。"""
        with self.assertRaises(RuntimeError):
            with session_scope() as db:
                StockRepository.batch_upsert(
                    db,
                    [{
                        "symbol": "000002", "date": "2024-01-02",
                        "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
                        "volume": 100, "amount": 110.0,
                    }],
                    commit=False,
                )
                raise RuntimeError("boom")

        with session_scope() as db:
            self.assertTrue(StockRepository.get_stock_df(db, "000002").empty)

    def test_borrowed_session_not_closed(self):
        db = self.Session()
        try:
            with session_scope(db) as inner:
                self.assertIs(inner, db)
            # 借用外部 session 时不应关闭，仍可继续使用
            self.assertEqual(len(StockRepository.get_stock_df(db, "000001")), 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
