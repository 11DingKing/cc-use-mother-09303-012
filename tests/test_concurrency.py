"""并发与恢复幂等性测试。"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from _fixtures import build_world, register_sample_world


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_dispatch_creates_exactly_one_set(self) -> None:
        world = build_world("2026-09-29")
        ctx = register_sample_world(world)
        store, clock = world["store"], world["clock"]

        def one_dispatch(_: int) -> dict:
            # 每个线程独立 Scheduler，共享同一存储（服务多副本/恢复语义）
            from obligation_service.scheduler import Scheduler

            return Scheduler(store, clock).dispatch()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one_dispatch, range(8)))

        created_total = sum(r["reminders_created"] for r in results)
        opened_total = sum(r["cases_opened"] for r in results)

        # 串行重放一遍，作为期望基准
        from obligation_service.scheduler import Scheduler

        baseline = Scheduler(store, clock).dispatch()
        self.assertEqual(baseline["reminders_created"], 0)
        self.assertEqual(baseline["cases_opened"], 0)

        # 8 个并发调度合计只产生基准数量：每个提醒点恰好一条
        equip = ctx["duties"]["EQUIP-DELIVERY"]
        equip_rows = store.fetchall(
            "SELECT kind, scheduled_for, COUNT(*) AS c FROM reminders WHERE duty_id=?"
            " GROUP BY kind, scheduled_for HAVING c > 1",
            (equip,),
        )
        self.assertEqual(equip_rows, [], "存在重复提醒")
        cases = store.fetchall(
            "SELECT window_key, COUNT(*) AS c FROM overdue_cases WHERE duty_id=?"
            " GROUP BY window_key HAVING c > 1",
            (equip,),
        )
        self.assertEqual(cases, [], "存在重复逾期案件")
        self.assertGreater(created_total, 0)
        self.assertGreater(opened_total, 0)

    def test_concurrent_transfer_dispatches_keep_single_chain(self) -> None:
        world = build_world("2026-09-29")
        ctx = register_sample_world(world)
        equip = ctx["duties"]["EQUIP-DELIVERY"]
        world["sch"].dispatch()

        errors: list[Exception] = []

        def transfer(i: int) -> None:
            try:
                world["reg"].transfer_ownership(world["admin"], equip, {
                    "new_owner_party": f"dept-{i % 3}",
                    "new_owner_contact": f"contact-{i}",
                    "reason": f"第{i}次移交",
                })
            except Exception as exc:  # 串行化冲突允许失败，但不得损坏链
                errors.append(exc)

        threads = [threading.Thread(target=transfer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 版本链连续无缺号、无重号
        versions = [r["version"] for r in world["store"].fetchall(
            "SELECT version FROM obligation_revisions WHERE duty_id=? ORDER BY version", (equip,))]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))
        self.assertEqual(len(versions), 1 + (6 - len(errors)))


if __name__ == "__main__":
    unittest.main()
