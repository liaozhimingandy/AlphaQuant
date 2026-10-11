#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dashboard 内联 JS/CSS 的回归护栏。

这些 bug 都是「肉眼难发现、跑起来才炸」的 frontend 问题，靠 Python 单测
抓不到，所以这里直接解析 dashboard.py 里那段 <script>/<style> 做结构性断言：

1. 不能有两个同名 function（曾经 loadStrategies 被回测加载器覆盖，
   导致策略库页 renderStrategies 永不执行、因子表为空、全选/清空没反应）。
2. 策略库加载器必须叫 loadStrategies 且调用 renderStrategies（/api/strategy-library）。
3. 回测策略下拉加载器改名 loadBtStrategies，不再抢 loadStrategies 这个名字。
4. .hidden 的 display:none 必须覆盖策略三个子表单 div（否则"切类型页面不变"）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "monitoring" / "static" / "dashboard.html"
SRC = DASH.read_text(encoding="utf-8")

SCRIPT = re.search(r"<script>(.*?)</script>", SRC, re.S).group(1)
STYLE = re.search(r"<style>(.*?)</style>", SRC, re.S).group(1)


class TestNoDuplicateFunctions(unittest.TestCase):
    def test_no_duplicate_function_names(self):
        names = re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", SCRIPT)
        from collections import Counter
        dup = {k: v for k, v in Counter(names).items() if v > 1}
        self.assertEqual(dup, {}, f"存在同名函数覆盖: {dup}")

    def test_load_strategies_is_the_library_loader(self):
        # 只有一个 loadStrategies，且它走 /api/strategy-library + renderStrategies
        self.assertEqual(
            len(re.findall(r"function\s+loadStrategies\s*\(", SCRIPT)), 1,
            "loadStrategies 只能有一个定义（策略库加载器）",
        )
        self.assertIn(
            "api('/api/strategy-library').then(renderStrategies)",
            SCRIPT, "策略库加载器必须调用 renderStrategies",
        )

    def test_backtest_strategy_loader_renamed(self):
        # 回测策略下拉加载器已改名，不再与策略库加载器抢名字
        self.assertIn("function loadBtStrategies(", SCRIPT)
        self.assertEqual(
            len(re.findall(r"function\s+loadStrategies\s*\(", SCRIPT)), 1,
        )
        # switchPage 进入回测页时会补调，保证 bt-strategy 下拉被填充
        self.assertIn("loadBtStrategies();", SCRIPT)


class TestHiddenCoversSubForms(unittest.TestCase):
    def test_hidden_hides_strategy_subforms(self):
        # 这条 display:none 规则必须覆盖三个策略子表单 div，否则切类型页面不变
        self.assertIn("#s-form-rule.hidden", STYLE)
        self.assertIn("#s-form-score.hidden", STYLE)
        self.assertIn("#s-form-multi.hidden", STYLE)

    def test_multi_custom_mode_reveals_weights(self):
        # 自定义权重模式下所有因子行都该显示权重框（CSS 选择器存在）
        self.assertIn(".factor-grid label.frow.custom .wbox", STYLE)


class TestPoolTableAlignment(unittest.TestCase):
    def test_pool_header_has_15_columns(self):
        # 打分表 8 维度（综合分/资金链/题材/筹码/技术/涨停/基本面/舆情/宏观）
        # 必须加上 #/代码/名称/等级/来源/操作。表头 = 1+1+1+1+8+1+1+1 = 15。
        blocks = re.findall(r"<thead><tr>(.*?)</tr></thead>", SRC, re.S)
        pool_block = next(b for b in blocks if "来源" in b)
        self.assertEqual(len(re.findall(r"<th", pool_block)), 15,
                         "股票池表头必须 15 列（含代码/名称/8维度/来源/操作）")
        # 名称列必须存在（否则 600042 这类代码无从辨识）
        self.assertIn("<th>名称</th>", pool_block, "股票池表头缺少「名称」列")
        # 数据行也要渲染名称（代码列之后）
        self.assertIn("'<td class=\"muted\">' + esc(r.name||'') + '</td>'", SCRIPT,
                      "数据行必须渲染标的名称")

    def test_pool_exclusion_checkboxes_present(self):
        # 4 个剔除条件勾选框必须存在
        for cid in ("pool-ex-st", "pool-ex-cyb", "pool-ex-kcb", "pool-ex-bj"):
            self.assertIn(f'id="{cid}"', SRC, f"缺少剔除条件勾选框 {cid}")

    def test_load_pool_appends_source_filter(self):
        # 选了候选池来源后，列表 GET 必须带上 source，否则"筛选出2条却显示15条"
        self.assertIn("$('pool-source').value", SCRIPT)
        self.assertIn("source=' + encodeURIComponent(src)", SCRIPT)
        # 切换来源下拉即刷新列表
        self.assertIn("if(el.id === 'pool-source'){ loadPool(); return; }", SCRIPT)

    def test_refresh_sends_exclusions(self):
        # 刷新必须把剔除条件 POST 给后端
        self.assertIn("exclusions: excl", SCRIPT)
        self.assertIn("exclude_cyb: $('pool-ex-cyb').checked", SCRIPT)

    def test_data_day_hint_on_closed_day(self):
        # 未开盘时面板要提示"显示为上一个交易日的数据"：
        # 1) 必须有 banner 元素 2) 渲染逻辑读取 is_historical/data_day
        # 3) 提示文案点明"非开盘时段"与"上一个交易日"
        self.assertIn('id="pool-datahint"', SRC, "缺少数据日提示条元素")
        self.assertIn("function renderPoolDataHint(", SCRIPT)
        self.assertIn("d.is_historical", SCRIPT, "renderPool 必须消费 is_historical")
        self.assertIn("d.data_day", SCRIPT, "renderPool 必须消费 data_day")
        self.assertIn("非开盘时段", SCRIPT, "提示需说明当前非开盘")
        self.assertIn("上一个交易日", SCRIPT, "提示需说明是上一个交易日数据")
        # banner 的 CSS 与配色变量也要有，否则提示条不可见
        self.assertIn(".banner{", STYLE)
        self.assertIn("--warn-bg", STYLE)


class TestDataGuardPanel(unittest.TestCase):
    """数据校验区块：缺一天的K线不会报错，面板是唯一能看见它的地方。"""

    def test_guard_section_present(self):
        self.assertIn('id="guard-rows"', SRC, "缺少数据校验表格")
        self.assertIn("function renderGuard(", SCRIPT)
        self.assertIn("function loadGuard(", SCRIPT)
        # 必须真的被初始加载与轮询（否则区块永远显示"加载中…"）
        self.assertIn("loadGuard();", SCRIPT)
        self.assertIn("setInterval(loadGuard", SCRIPT)

    def test_guard_manual_check_button(self):
        self.assertIn('id="btn-guard-check"', SRC)
        self.assertIn("post('/api/dataguard/check'", SCRIPT)
        # 体检要能选择"只体检不补"
        self.assertIn('id="guard-repair"', SRC)
        self.assertIn("auto_repair: !!$('guard-repair').checked", SCRIPT)

    def test_guard_renders_missing_and_source(self):
        # 缺哪几天、补了多少、用的哪个源 —— 三件事缺一不可
        self.assertIn("s.missing", SCRIPT)
        self.assertIn("s.repaired", SCRIPT)
        self.assertIn("s.source", SCRIPT)
        self.assertIn("疑似停牌", SCRIPT, "停牌票要能识别，别当成漏采")

    def test_guard_table_header_does_not_shadow_pool(self):
        """新表头不能含「来源」二字。

        股票池表头那条断言是用 "第一个含「来源」的 thead" 定位的，
        如果校验表也写「来源」，会先命中校验表（8 列）而不是股票池（15 列）。
        """
        blocks = re.findall(r"<thead><tr>(.*?)</tr></thead>", SRC, re.S)
        for b in blocks:
            if "已补" in b:
                self.assertNotIn("来源", b)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
