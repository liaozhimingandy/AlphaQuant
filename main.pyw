#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : main.pyw
# @Description : 无控制台入口（Windows）
#
#                Windows 把 .pyw 关联到 pythonw.exe（GUI 子系统程序），
#                所以**双击这个文件不会弹出控制台黑框**。
#
#                两种用法：
#                  1. 直接双击 —— 等价于 `python main.py start`，
#                     后台起服务，然后弹一个提示框告诉你面板地址。
#                  2. 命令行 `python main.pyw <命令>` —— 与 main.py 完全一样，
#                     只是此时输出由调用方终端承担（想看输出请用 main.py）。
#
#                注意区分两件事：
#                  - 本文件解决的是"**启动器**不弹黑框"
#                  - 后台服务进程不弹黑框由 daemon.resolve_python 负责
#                    （它会在 venv 里挑 pythonw.exe 来拉起服务进程）
#                两处都做了，才真正做到全程无黑框。
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
import os
import sys


def _notify(title: str, message: str) -> None:
    """GUI 环境下给个反馈。Windows 用原生消息框（零依赖），其它平台只打印。"""
    if os.name == "nt":
        try:
            import ctypes

            # MB_OK | MB_ICONINFORMATION（0x40）：不使用 tkinter，避免为了
            # 一个提示框引入 GUI 依赖 —— 服务端程序不该依赖图形库。
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
            return
        except Exception:
            pass
    print(f"{title}: {message}")


def main() -> int:
    argv = sys.argv[1:] or ["start"]
    sys.argv = [sys.argv[0], *argv]

    from app.core.config import settings

    exit_code = 0
    try:
        from app.cli import cli

        # standalone_mode=False：不让 click 直接 sys.exit，
        # 否则后面的提示框就弹不出来了。
        cli.main(args=argv, standalone_mode=False)
    except SystemExit as exc:  # click 内部仍可能抛
        exit_code = int(exc.code or 0)
    except Exception as exc:
        exit_code = 1
        _notify("AlphaQuant 启动失败", str(exc))

    if exit_code == 0 and argv and argv[0] in ("start", "restart"):
        _notify(
            "AlphaQuant",
            f"服务已在后台运行（无控制台窗口）。\n\n"
            f"监控面板: http://{settings.MONITOR_HOST}:{settings.MONITOR_PORT}/\n"
            f"日志: {settings.DAEMON_LOG}\n\n"
            f"停止服务: 运行 scripts\\windows\\stop_hidden.vbs",
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
