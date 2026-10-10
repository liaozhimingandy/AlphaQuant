# 自定义策略目录

放进这个目录的 `.py` 文件会被**自动发现**，无需注册、无需改框架代码：

| 你写的类 | 用在哪 |
| --- | --- |
| 继承 `backtrader.Strategy` | `python main.py backtest --strategy <名字>` 与网页面板「回测监控」 |
| 继承 `app.core.strategy.base.IBaseStrategy` | 实盘/模拟引擎（`config/tasks.json` 里的 `"strategy": {"type": "<名字>"}`） |

两条链路**共用这一套发现机制**，所以你可以把同一份策略逻辑写成两个类放在一个文件里：
回测验证过的参数，直接用于实盘。

## 命名

注册名优先取类里的 `STRATEGY_NAME`（推荐，改名不影响配置）；
没有声明时由类名推导：`DualMaStrategy` → `dual_ma`。

```bash
python main.py strategies                 # 看全部（含自定义）
python main.py strategies --reload        # 重新扫描（改完代码不用重启服务）
python main.py ctl strategies             # 对运行中的服务热重载
```

## 不想写代码？

用内置的**声明式策略** `declarative`，把规则写成 JSON 就能回测：

```bash
python main.py backtest -s 000001 --strategy declarative \
  --param entry='{"cross_up":{"left":"ma","right":"ma","left_params":{"period":5},"right_params":{"period":20}}}' \
  --param exit='{"cross_down":{"left":"ma","right":"ma","left_params":{"period":5},"right_params":{"period":20}}}'
```

或者把规则写进文件，再引用它：

```bash
python main.py backtest -s 000001 --strategy declarative --param entry_file=strategies/my_rules.json
```

网页面板的「回测监控」页选中 `declarative` 后，会多出一个「规则定义(JSON)」输入框，
在里面贴 `{"entry": {...}, "exit": {...}}` 即可。

## 注意

- 文件加载失败（语法错误、缺少依赖）**只会跳过该文件并记日志**，不会让程序起不来。
  如果你发现自己的策略没出现在清单里，先看日志里的 `加载用户策略 xxx.py 失败`。
- 本目录与框架代码分离，**升级框架不会覆盖这里**。
