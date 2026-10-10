#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券商接入点 / 网关构造 / 实盘账户读取 的单元测试。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.core.execution.endpoints import (
    BrokerEndpoint,
    load_endpoints,
    mask_secret,
)
from app.core.execution.gateway import (
    OrderRequest,
    OrderRejected,
    SimulatedGateway,
    create_gateway,
    gateway_signature,
    has_gateway,
    list_gateways,
)
from app.core.market.types import Side
from app.test.helpers import TempWorkspaceMixin


class TestMaskSecret(unittest.TestCase):
    def test_masks_middle(self):
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("ab"), "**")
        self.assertEqual(mask_secret("abcd"), "****")
        m = mask_secret("supersecret123")
        self.assertTrue(m.startswith("su") and m.endswith("23"))
        self.assertNotIn("persecr", m)

    def test_none_is_empty(self):
        self.assertEqual(mask_secret(None), "")


class TestBrokerEndpoint(unittest.TestCase):
    def test_gateway_kwargs_includes_account_and_credentials(self):
        ep = BrokerEndpoint(
            id="a", gateway="simulated", account="123456",
            credential_env={"password": "AQ_TEST_PWD"},
        )
        kw = ep.gateway_kwargs(env={"AQ_TEST_PWD": "p@ss"})
        self.assertEqual(kw["account"], "123456")
        self.assertEqual(kw["password"], "p@ss")
        self.assertEqual(kw["endpoint_id"], "a")
        self.assertFalse(kw["readonly"])

    def test_missing_credential_does_not_raise(self):
        """凭据缺失只告警不抛错：本地模拟网关本来就不需要凭据。"""
        ep = BrokerEndpoint(id="a", credential_env={"password": "NOT_SET_XYZ"})
        kw = ep.gateway_kwargs(env={})
        self.assertNotIn("password", kw)

    def test_to_dict_masks_gateway_kwargs(self):
        ep = BrokerEndpoint(id="a", account="888", params={"token": "abcdef123456"})
        ep.resolve_credentials(env={})
        d = ep.to_dict()
        self.assertEqual(d["account"], "888")
        self.assertNotEqual(d["params"]["token"], "abcdef123456")
        self.assertIn("*", d["params"]["token"])

    def test_to_dict_reports_env_status(self):
        ep = BrokerEndpoint(id="a", credential_env={"password": "AQ_X"})
        d = ep.to_dict()
        self.assertEqual(d["credentials"]["password"]["env"], "AQ_X")
        self.assertIn("set", d["credentials"]["password"])

    def test_from_dict_explicit_empty_gateway_stays_empty(self):
        """显式写空串 ≠ 没写。要保持空，才能报"这个接入点还没配网关"。"""
        ep = BrokerEndpoint.from_dict("a", {"gateway": ""})
        self.assertEqual(ep.gateway, "")
        ep2 = BrokerEndpoint.from_dict("b", {})
        self.assertEqual(ep2.gateway, "simulated")


class TestEndpointsConfig(TempWorkspaceMixin, unittest.TestCase):
    def _write(self, data) -> str:
        p: Path = self.workdir() / "brokers.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_load_from_file(self):
        path = self._write({
            "default_endpoint": "main",
            "endpoints": {
                "main": {"gateway": "simulated", "account": "1", "name": "主"},
                "off": {"gateway": "simulated", "enabled": False},
            },
        })
        cfg = load_endpoints(path)
        self.assertEqual(cfg.default_endpoint, "main")
        self.assertEqual(sorted(cfg.endpoints), ["main", "off"])
        self.assertEqual(cfg.get().id, "main")
        self.assertEqual([e.id for e in cfg.list(only_enabled=True)], ["main"])

    def test_explicit_missing_endpoint_raises(self):
        """显式指定的接入点找不到必须报错。

        静默降级是实盘里最危险的行为：用户以为连的是 A，实际连了别处。
        """
        path = self._write({"endpoints": {"a": {"gateway": "simulated"}}})
        cfg = load_endpoints(path)
        self.assertIsNotNone(cfg.resolve("") or None)  # 默认项可以没有
        with self.assertRaises(KeyError):
            cfg.resolve("no_such")

    def test_missing_file_falls_back_to_env_gateway(self):
        cfg = load_endpoints(str(self.workdir("nope") / "missing.json"))
        self.assertEqual(cfg.endpoints, {})  # 没有 BROKER_GATEWAY 时就是空配置
        self.assertIsNone(cfg.get())

    def test_default_endpoint_picks_first_enabled(self):
        path = self._write({
            "endpoints": {
                "b": {"gateway": "simulated", "enabled": True},
                "a": {"gateway": "simulated", "enabled": False},
            }
        })
        cfg = load_endpoints(path)
        self.assertEqual(cfg.default_endpoint, "b")

    def test_broken_json_is_not_fatal(self):
        p = self.workdir() / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        cfg = load_endpoints(str(p))
        self.assertEqual(cfg.endpoints, {})


class TestGatewayFactory(unittest.TestCase):
    def test_simulated_registered(self):
        self.assertIn("simulated", list_gateways())
        self.assertTrue(has_gateway("simulated"))
        self.assertFalse(has_gateway("nope"))

    def test_unknown_kwargs_are_dropped_not_crashed(self):
        """接入点参数是通用的，各家网关不一定都用得上 —— 必须过滤而不是报错。

        否则"多配了一个字段就连不上"，而报错是 TypeError，很难联想到是配置问题。
        """
        gw = create_gateway("simulated", account="1", readonly=True,
                            totally_unknown_option="x")
        self.assertEqual(gw.account, "1")
        self.assertTrue(gw.readonly)

    def test_signature_lists_params(self):
        sig = gateway_signature("simulated")
        self.assertIn("account", sig)
        self.assertIn("readonly", sig)
        self.assertIn("initial_cash", sig)

    def test_readonly_blocks_orders(self):
        gw = create_gateway("simulated", readonly=True)
        gw.connect()
        try:
            with self.assertRaises(OrderRejected):
                gw.place_order(OrderRequest(symbol="000001", side=Side.BUY,
                                            size=100, price=10.0))
            self.assertEqual(gw.stats["blocked_by_readonly"], 1)
            self.assertEqual(gw.stats["placed"], 0)
        finally:
            gw.disconnect()

    def test_cancel_flows_back_as_order_update(self):
        """撤单是异步的：本地撤单后要等券商状态回报才真正生效。"""
        gw = SimulatedGateway(fill_delay_calls=99)
        gw.connect()
        bid = gw.place_order(OrderRequest(symbol="000001", side=Side.BUY,
                                          size=100, price=10.0))
        self.assertEqual(len(gw.query_orders(only_open=True)), 1)
        self.assertTrue(gw.cancel_order(bid))
        self.assertEqual(len(gw.query_orders(only_open=True)), 0)
        updates = gw.poll_order_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].broker_order_id, bid)

    def test_query_account_and_positions_reflect_fills(self):
        gw = SimulatedGateway(price_source=lambda: {"000001": 10.0},
                              fill_delay_calls=0, initial_cash=100000.0)
        gw.connect()
        gw.place_order(OrderRequest(symbol="000001", side=Side.BUY,
                                    size=1000, price=10.0))
        fills = gw.poll_fills()
        self.assertEqual(len(fills), 1)
        acct = gw.query_account()
        self.assertLess(acct.cash, 100000.0)
        pos = gw.query_positions()["000001"]
        self.assertEqual(pos.size, 1000)
        self.assertEqual(pos.frozen_size, 1000)   # T+1：当日买入冻结
        self.assertEqual(pos.sellable, 0)


if __name__ == "__main__":
    unittest.main()
