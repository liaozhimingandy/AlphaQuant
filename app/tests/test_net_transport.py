#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一网络传输层 / 域名健康度 / 人气榜可用源的回归测试。

这一组测试守的是"取不到数据时能不能自己说清为什么"：
请求头有没有带、代理挂了会不会自动换直连、同一个坏域名会不会被反复等超时、
以及人气榜有没有一个**真的能出数**的源。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

from app.datasource.transport import net
from app.datasource.compat.registry import REGISTRY, SourceError, SourceRegistry


def _ok_df(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame({"symbol": [f"00000{i}" for i in range(1, n + 1)]})


class TestHeaders(unittest.TestCase):
    def test_eastmoney_gets_referer(self):
        h = net.headers_for("https://emappdata.eastmoney.com/x")
        self.assertEqual(h["Referer"], "https://quote.eastmoney.com/")
        self.assertIn("Mozilla", h["User-Agent"])

    def test_tencent_gets_referer(self):
        h = net.headers_for("https://qt.gtimg.cn/q=sh600000")
        self.assertEqual(h["Referer"], "https://gu.qq.com/")

    def test_unknown_host_has_no_referer(self):
        h = net.headers_for("https://example.com/api")
        self.assertNotIn("Referer", h)

    def test_extra_headers_win(self):
        h = net.headers_for("https://emappdata.eastmoney.com/x",
                            {"Referer": "https://my.ref/"})
        self.assertEqual(h["Referer"], "https://my.ref/")

    def test_host_of(self):
        self.assertEqual(net.host_of("https://a.b.com:443/x?y=1"), "a.b.com")
        self.assertEqual(net.host_of("not a url"), "")


class TestHostHealth(unittest.TestCase):
    def setUp(self):
        net.reset()

    def tearDown(self):
        net.reset()

    def test_two_failures_block_host(self):
        net.mark("h1.com", False, "boom")
        self.assertFalse(net.is_blocked("h1.com"), "一次失败不该直接判死")
        net.mark("h1.com", False, "boom again")
        self.assertTrue(net.is_blocked("h1.com"))

    def test_success_clears_block(self):
        net.mark("h2.com", False, "x")
        net.mark("h2.com", False, "x")
        self.assertTrue(net.is_blocked("h2.com"))
        net.mark("h2.com", True)
        self.assertFalse(net.is_blocked("h2.com"))

    def test_blocked_reason_readable(self):
        net.mark("h3.com", False, "ProxyError")
        net.mark("h3.com", False, "ProxyError")
        reason = net.blocked_reason(["h3.com", "fine.com"])
        self.assertIn("h3.com", reason)
        self.assertIn("ProxyError", reason)
        self.assertNotIn("fine.com", reason)

    def test_blocked_reason_empty_when_no_hosts(self):
        self.assertEqual(net.blocked_reason([]), "")
        self.assertEqual(net.blocked_reason(None), "")

    def test_host_state_shape(self):
        net.mark("h4.com", True)
        st = net.host_state()["h4.com"]
        self.assertEqual(st["ok"], 1)
        self.assertFalse(st["blocked"])


class TestTransportFallback(unittest.TestCase):
    """代理挂了要自动改直连 —— 僵尸代理最典型的症状。"""

    def setUp(self):
        net.reset()
        self._old = net.offline()
        net.offline(False)

    def tearDown(self):
        net.reset()
        net.offline(self._old)

    def test_falls_back_to_direct_when_proxy_fails(self):
        calls = []

        def fake(method, url, **kw):
            calls.append(kw.get("proxies"))
            if len(calls) == 1:
                raise ConnectionError("proxy dead")
            resp = mock.MagicMock()
            resp.raise_for_status.return_value = None
            return resp

        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:9"}):
            with mock.patch.object(net.requests, "request", fake):
                net.request("GET", "https://a.com/x")
        self.assertEqual(len(calls), 2, "代理失败后应再试一次直连")
        self.assertIsNone(calls[0], "第一次走代理（不覆盖环境变量）")
        self.assertEqual(calls[1], {"http": "", "https": ""}, "第二次绕开代理")

    def test_no_direct_fallback_without_proxy(self):
        """没配代理就别自作主张尝试直连 —— 那只是多一次没意义的等待。"""
        calls = []

        def fake(method, url, **kw):
            calls.append(kw.get("proxies"))
            raise ConnectionError("down")

        env = {k: v for k, v in __import__("os").environ.items()
               if k.lower() not in ("http_proxy", "https_proxy")}
        # 同时关掉自动探测：本机若有 Clash 在监听，会被当成"已配代理"，
        # 于是先试代理再试直连，变成 2 次调用 —— 本用例要测的是"无代理"场景。
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(net, "_autodetect_proxy", return_value=""):
            with mock.patch.object(net.requests, "request", fake):
                with self.assertRaises(net.NetworkError):
                    net.request("GET", "https://a.com/x")
        self.assertEqual(len(calls), 1)

    def test_offline_mode_blocks_real_requests(self):
        net.offline(True)
        with self.assertRaises(net.NetworkError):
            net.request("GET", "https://a.com/x")

    def test_blocked_host_fails_fast_without_calling(self):
        net.mark("dead.com", False, "x")
        net.mark("dead.com", False, "x")
        with mock.patch.object(net.requests, "request",
                               side_effect=AssertionError("不该发请求")):
            with self.assertRaises(net.HostBlocked):
                net.request("GET", "https://dead.com/x")


class TestZombieProxy(unittest.TestCase):
    """僵尸代理（端口没人监听）必须被自动摘除。

    akshare/baostock 自己发请求、只认环境变量 —— 传输层里再怎么"代理失败改直连"
    也救不了它们，所以这一刀必须砍在环境变量上。
    """

    def setUp(self):
        net.reset()
        self._flag = net.offline()

    def tearDown(self):
        net.reset()
        net.offline(self._flag)

    def _reset_cache(self):
        net._proxy_checked = False
        net._proxy_state = None

    def test_dead_proxy_is_removed(self):
        env = {"HTTP_PROXY": "http://127.0.0.1:59999",
               "HTTPS_PROXY": "http://127.0.0.1:59999",
               "http_proxy": "http://127.0.0.1:59999",
               "https_proxy": "http://127.0.0.1:59999"}
        self._reset_cache()
        with mock.patch.dict("os.environ", env):
            with mock.patch.object(net, "proxy_reachable", return_value=False):
                self.assertFalse(net.sanitize_proxy())
                self.assertIsNone(__import__("os").environ.get("HTTP_PROXY"))
                self.assertIsNone(__import__("os").environ.get("http_proxy"))

    def test_alive_proxy_is_kept(self):
        self._reset_cache()
        env = {"HTTPS_PROXY": "http://127.0.0.1:7890"}
        with mock.patch.dict("os.environ", env):
            with mock.patch.object(net, "proxy_reachable", return_value=True):
                self.assertTrue(net.sanitize_proxy())
                self.assertEqual(__import__("os").environ["HTTPS_PROXY"],
                                 "http://127.0.0.1:7890")

    def test_checked_only_once(self):
        """TCP 探测有成本，不能每次请求都做一遍。"""
        self._reset_cache()
        env = {"HTTPS_PROXY": "http://127.0.0.1:7890"}
        with mock.patch.dict("os.environ", env):
            with mock.patch.object(net, "proxy_reachable",
                                   return_value=True) as fn:
                net.sanitize_proxy()
                net.sanitize_proxy()
                net.sanitize_proxy()
        self.assertEqual(fn.call_count, 1)

    def test_no_proxy_means_nothing_to_do(self):
        self._reset_cache()
        env = {k: v for k, v in __import__("os").environ.items()
               if k.lower() not in ("http_proxy", "https_proxy")}
        # 关掉自动探测，否则本机真在监听的代理端口会让"无代理"前提不成立
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(net, "_autodetect_proxy", return_value=""):
            self.assertIsNone(net.sanitize_proxy())


class TestRegistryHostSkip(unittest.TestCase):
    """声明了依赖域名的源，域名判死后要被直接跳过。"""

    def setUp(self):
        net.reset()
        REGISTRY.reset()

    def tearDown(self):
        net.reset()
        REGISTRY.reset()

    def _reg(self):
        reg = SourceRegistry(circuit_threshold=99, cooldown=0.0, retries=0)
        return reg

    def test_hosts_recorded_on_register(self):
        reg = self._reg()
        reg.register("a", supports=["k"], fetch=lambda: _ok_df(),
                     hosts=["x.com"])
        self.assertEqual(reg.sources_for("k")[0].hosts, {"x.com"})

    def test_source_skipped_when_host_blocked(self):
        reg = self._reg()
        net.mark("bad.com", False, "ProxyError")
        net.mark("bad.com", False, "ProxyError")

        def boom():
            raise AssertionError("域名已死，不该真的发请求")

        reg.register("dead_src", supports=["k"], fetch=boom, hosts=["bad.com"])
        reg.register("good_src", supports=["k"], fetch=lambda: _ok_df())
        df = reg.fetch("k")
        self.assertEqual(len(df), 2)

        # 被跳过的源要留下一条可读的事件，否则日志里查不到"为什么没试它"
        evs = [e for e in reg.recent_events(10) if e["source"] == "dead_src"]
        self.assertTrue(evs)
        self.assertIn("bad.com", evs[0]["error"])

    def test_all_sources_skipped_raises_source_error(self):
        reg = self._reg()
        net.mark("bad.com", False, "e")
        net.mark("bad.com", False, "e")
        reg.register("dead_src", supports=["k"],
                     fetch=lambda: (_ for _ in ()).throw(AssertionError("no")),
                     hosts=["bad.com"])
        with self.assertRaises(SourceError):
            reg.fetch("k")

    def test_failure_marks_declared_hosts(self):
        """akshare 自己发请求，不走 net.request —— 必须靠回写才知道域名挂了。"""
        reg = self._reg()

        def boom():
            raise ConnectionError("push2 被拦")

        reg.register("ak", supports=["k"], fetch=boom, hosts=["push2.x.com"])
        with self.assertRaises(SourceError):
            reg.fetch("k")
        # 一次源级失败记一次，达到阈值后域名进入冷却
        self.assertEqual(net.host_state()["push2.x.com"]["fail"], 1)

    def test_success_marks_hosts_ok(self):
        reg = self._reg()
        net.mark("ok.com", False, "x")
        reg.register("s", supports=["k"], fetch=lambda: _ok_df(), hosts=["ok.com"])
        reg.fetch("k")
        self.assertFalse(net.is_blocked("ok.com"))


class TestEmappHotSource(unittest.TestCase):
    """人气榜必须有一个不依赖 push2 的可用源。"""

    def test_emapp_hot_registered_first(self):
        import app.datasource.service.facade  # noqa: F401  触发注册

        names = [s.name for s in REGISTRY.sources_for("hot_rank")]
        self.assertEqual(names[0], "emapp_hot",
                         "人气榜首个源必须是实测可用的 emapp_hot，不是压在 push2 上的源")

    def test_emapp_hot_not_depending_on_push2(self):
        import app.datasource.service.facade  # noqa: F401

        spec = [s for s in REGISTRY.sources_for("hot_rank")
                if s.name == "emapp_hot"][0]
        self.assertFalse(any("push2" in h for h in spec.hosts))

    def test_parse_emapp_payload(self):
        from app.datasource.compat import hot_sources

        body = {"data": [
            {"sc": "SH600418", "rk": 1},
            {"sc": "SZ002475", "rk": 2},
            {"sc": "HK00700", "rk": 3},      # 港股 → 剔除
            {"sc": "SH600", "rk": 4},        # 代码不合法 → 剔除
        ]}
        with mock.patch.object(hot_sources.net, "post_json", return_value=body), \
             mock.patch.object(hot_sources, "fetch_tx_quotes", return_value={}), \
             mock.patch("app.storage.repo.kline_repo.resolve_names",
                        return_value={}):
            df = hot_sources._hot_rank_emapp(top=10)
        self.assertEqual(list(df["symbol"]), ["600418", "002475"])
        self.assertEqual(list(df["rank"]), [1, 2])

    def test_empty_payload_is_data_unavailable(self):
        from app.datasource.compat import hot_sources
        from app.datasource.compat.registry import DataUnavailable

        with mock.patch.object(hot_sources.net, "post_json", return_value={"data": []}):
            with self.assertRaises(DataUnavailable):
                hot_sources._emapp_rank()

    def test_network_error_propagates_for_switch(self):
        """域名挂了要抛出可重试的异常，让注册表换下一个源。"""
        from app.datasource.compat import hot_sources

        with mock.patch.object(hot_sources.net, "post_json",
                               side_effect=net.NetworkError("域名凉了")):
            with self.assertRaises(net.NetworkError):
                hot_sources._emapp_rank()

    def test_tx_quote_parsing(self):
        from app.datasource.compat import hot_sources

        text = ('v_sh600000="1~浦发银行~600000~9.54~9.70~9.70~947958~";'
                'v_sz000001="1~平安银行~000001~11.00~10.00~10.10~123~";')
        with mock.patch.object(hot_sources.net, "get_text", return_value=text):
            out = hot_sources.fetch_tx_quotes(["600000", "000001"])
        self.assertEqual(out["600000"]["name"], "浦发银行")
        self.assertAlmostEqual(out["600000"]["pct_change"], -1.65, places=1)
        self.assertAlmostEqual(out["000001"]["pct_change"], 10.0, places=1)

    def test_tx_quote_failure_degrades_not_raise(self):
        """补不了名称可以降级，不该把已经拿到的榜单丢掉。"""
        from app.datasource.compat import hot_sources

        with mock.patch.object(hot_sources.net, "get_text",
                               side_effect=net.NetworkError("腾讯也挂了")):
            self.assertEqual(hot_sources.fetch_tx_quotes(["600000"]), {})


class TestPoolRankAndBanner(unittest.TestCase):
    """股票池表格的序号与告警条占位。"""

    def setUp(self):
        import re

        # 面板 HTML 已从「2501 行的 .py」搬回它该在的位置，走加载器读取
        from app.monitoring.dashboard import load_dashboard_html

        src = load_dashboard_html()
        self.script = re.search(r"<script>(.*?)</script>", src, re.S).group(1)
        self.style = re.search(r"<style>(.*?)</style>", src, re.S).group(1)

    def test_rank_is_row_index_not_db_rank(self):
        """序号必须按当前表格顺序从 1 编，不能直接取库里的 rank。"""
        self.assertIn("rows.map(function(r, i)", self.script)
        self.assertIn("(i + 1)", self.script)
        # 渲染池表那一段里不该再出现 r.rank
        body = self.script.split("function renderPool(")[1].split("function loadPool(")[0]
        self.assertNotIn("r.rank", body)

    def test_banner_hidden_rule_exists(self):
        """没告警时提示条必须真的收起，不能留一块黄/红占位。"""
        self.assertIn(".banner.hidden{display:none}", self.style.replace(" ", ""))

    def test_warn_box_cleared_when_no_warnings(self):
        self.assertIn("box.classList.add('hidden')", self.script)
        self.assertIn("box.innerHTML = ''", self.script)


class TestProxyResolution(unittest.TestCase):
    """代理的解析顺序：显式配置 > 环境变量 > 自动探测。

    为什么要有显式配置这一层：采集服务常以守护进程方式启动，
    **不继承** shell 里的 HTTP_PROXY，于是直连东财被 RST，
    报"所有源都失败"，而手动在命令行跑却是好的 —— 极难定位。
    """

    def setUp(self):
        self._saved = {k: v for k, v in __import__("os").environ.items()
                       if k.lower() in ("http_proxy", "https_proxy")}

    def tearDown(self):
        import os
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            if k in self._saved:
                os.environ[k] = self._saved[k]
            else:
                os.environ.pop(k, None)

    def test_explicit_config_beats_env(self):
        """DATA_PROXY 是本项目明确意图，必须压过继承来的环境变量。"""
        from app.common.config import settings

        env = {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
               "HTTPS_PROXY": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1"}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(settings, "DATA_PROXY", "http://127.0.0.1:7890"), \
             mock.patch.object(net, "_autodetect_proxy", return_value=""):
            self.assertEqual(net._env_proxy(), "http://127.0.0.1:7890")

    def test_ensure_proxy_writes_env_for_akshare(self):
        """必须写回环境变量 —— akshare 内部自己发请求，只认环境变量。

        只在传输层传 proxies= 会造成"一半走代理、一半直连"的诡异现象。
        """
        from app.common.config import settings
        import os

        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy")}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(settings, "DATA_PROXY", "http://127.0.0.1:7890"), \
             mock.patch.object(net, "_autodetect_proxy", return_value=""):
            got = net.ensure_proxy()
            # 断言必须在 patch 内：退出 with 后 mock 会把环境变量还原
            self.assertEqual(got, "http://127.0.0.1:7890")
            self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:7890")
            self.assertEqual(os.environ.get("https_proxy"), "http://127.0.0.1:7890")

    def test_autodetect_used_only_as_last_resort(self):
        """自动探测只在前面都没有时才用。"""
        from app.common.config import settings

        env = {"HTTP_PROXY": "http://127.0.0.1:9999"}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(settings, "DATA_PROXY", ""), \
             mock.patch.object(net, "_autodetect_proxy",
                               return_value="http://127.0.0.1:7890") as fn:
            self.assertEqual(net._env_proxy(), "http://127.0.0.1:9999")
            fn.assert_not_called()

    def test_autodetect_returns_loopback_url(self):
        """探测结果必须是回环地址 —— 绝不能把请求引到别的机器。"""
        found = net._autodetect_proxy()
        if found:
            self.assertTrue(found.startswith("http://127.0.0.1:")
                            or found.startswith("http://localhost:"), found)


if __name__ == "__main__":
    unittest.main()
