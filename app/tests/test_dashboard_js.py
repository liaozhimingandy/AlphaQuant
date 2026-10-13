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
        # 刷新必须把剔除条件 POST 给后端，而且要**现读勾选框**
        # （用缓存副本会出现"明明勾了却没剔除"）
        self.assertIn("exclusions: excl", SCRIPT)
        self.assertIn("function poolExclusionPayload(", SCRIPT)
        for name in ("exclude_st", "exclude_cyb", "exclude_kcb", "exclude_bj"):
            self.assertIn(name + ":", SCRIPT, f"剔除入参缺少 {name}")

    def test_exclusion_note_follows_checkboxes(self):
        """勾选完右侧说明必须立刻跟着变，且**不会**被轮询覆盖。

        以前右侧文案只在 renderPool() 里重算，而 renderPool 又被
        syncPoolExclusions 反手把勾选框改回 config 的值 —— 于是
        "勾了创业板但右边只显示 ST"，第二次轮询还会把勾取消掉。
        """
        self.assertIn("function renderPoolExNote(", SCRIPT)
        self.assertIn("POOL_EX_SYNCED", SCRIPT)
        self.assertIn("if(POOL_EX_SYNCED) { renderPoolExNote(); return; }", SCRIPT,
                      "后端配置只能同步一次，之后不许覆盖用户勾选")
        self.assertIn(
            "POOL_EX_FIELDS.some(function(f){ return f.id === el.id; })",
            SCRIPT, "勾选变化必须重算说明文案",
        )


class TestEngineTaskPolling(unittest.TestCase):
    """面板轮询引擎任务的正确性护栏。

    这里是两个已经踩过的坑，都属于"跑起来才炸、日志里还看不出来"：

    1. **接口名写错**：JS 里写 ``/api/engine_tasks``，后端根本没有这条路由，
       于是每次刷新都弹「查询任务失败: 未知接口」，而任务其实早跑完了。
    2. **状态大小写错**：store 里的状态是小写（``success``/``dead_letter``），
       拿 ``'SUCCESS'`` 去比永远不等，轮询只能转满 10 分钟才超时退出。
    """

    def test_does_not_call_unknown_endpoint(self):
        self.assertNotIn("/api/engine_tasks'", SCRIPT,
                         "后端没有 /api/engine_tasks 这条路由（会 404）")
        self.assertIn("function engineTaskUrl(", SCRIPT)
        self.assertIn("'/api/task?id='", SCRIPT)

    def test_status_compares_lowercase_or_terminal_map(self):
        # 不要再出现 === 'SUCCESS' / 'DEAD_LETTER' 这类永假比较
        for bad in ("=== 'SUCCESS'", "=== 'FAILED'", "=== 'DEAD_LETTER'",
                    "=== 'CANCELLED'"):
            self.assertNotIn(bad, SCRIPT, f"状态比较写错了大小写: {bad}")
        self.assertIn("TASK_TERMINAL", SCRIPT)
        self.assertIn("=== 'success'", SCRIPT)

    def test_failed_is_not_a_terminal_state(self):
        """``failed`` 永远不会停留：

        失败会立刻转成 ``retrying`` 或 ``dead_letter``，所以轮询的终态集合
        里不能包含 failed（见 store.py 的 STATUS_GROUPS）。
        """
        m = re.search(r"TASK_TERMINAL\s*=\s*\{(.*?)\}", SCRIPT, re.S)
        self.assertIsNotNone(m, "缺少 TASK_TERMINAL 终态定义")
        self.assertIn("success", m.group(1))
        self.assertIn("dead_letter", m.group(1))
        self.assertIn("cancelled", m.group(1))
        self.assertNotIn("failed", m.group(1))

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


class TestLedgerFilterControls(unittest.TestCase):
    """任务明细：什么时候必须真的去查数据库。

    用户侧的原始现象是"选了日期点刷新，一条记录都没有"。三个成因
    各留一道护栏：

    1. 表格以前会被 ``uiBusy()`` 拦掉 —— 日期框选完焦点还在框里，
       于是请求发了、汇总更新了，表格却一行没动；
    2. 筛选框一改就自动发请求，和「刷新」按钮各说各话，
       界面上的数据到底对应哪套条件说不清；
    3. 起止日期填反了会查出一个注定为空的区间。
    """

    def test_result_shows_the_filters_that_produced_it(self):
        # "没查出来"最需要知道的是"按什么条件查的"
        self.assertIn("查询条件：", SCRIPT)
        self.assertIn("function ledgerFiltersText(", SCRIPT)

    def test_button_is_search_not_refresh(self):
        self.assertIn('id="btn-rt-ledger"', SRC)
        self.assertIn('id="btn-rt-ledger-reset"', SRC)
        block = re.search(
            r'<button id="btn-rt-ledger"[^>]*>(.*?)</button>', SRC, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(1).strip(), "搜索")

    def test_explicit_search_bypasses_ui_busy(self):
        self.assertIn(
            "if(b.id === 'btn-rt-ledger'){ loadLedger({force: true}); return; }",
            SCRIPT, "点搜索必须真的重绘表格")
        self.assertIn("function loadLedger(opts){", SCRIPT)
        self.assertIn("if(!force && uiBusy()) return;", SCRIPT,
                      "自动轮询让位给 uiBusy()，显式查询必须无视它")

    def test_polling_stays_unforced(self):
        self.assertIn("if(PAGE === 'tasks') loadLedger(opts);", SCRIPT)
        self.assertIn("rtTickDetails({force: true})", SCRIPT,
                      "切进页面=明确要看，必须渲染")
        self.assertIn(
            "setInterval(function(){ loadRtCore(); rtTickDetails(); }", SCRIPT)

    def test_filter_change_only_marks_dirty(self):
        self.assertIn("var LEDGER_FILTER_IDS", SCRIPT)
        self.assertIn("function ledgerMarkDirty(", SCRIPT)
        self.assertIn(
            "LEDGER_FILTER_IDS.indexOf(el.id) >= 0){ ledgerMarkDirty(true); return; }",
            SCRIPT, "改筛选条件不该自动发请求")
        # 关键字不再边打边查，改成回车查
        self.assertNotIn("ledgerKwTimer = setTimeout(loadLedger, 600)", SCRIPT)

    def test_reversed_date_range_is_swapped(self):
        self.assertIn("if(since && until && since > until){", SCRIPT)
        self.assertIn("起止日期填反了，已自动调换", SCRIPT)

    def test_dirty_state_is_visible(self):
        self.assertIn('id="rt-ledger-dirty"', SRC)
        self.assertIn("条件已改，点「搜索」查询", SCRIPT)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
