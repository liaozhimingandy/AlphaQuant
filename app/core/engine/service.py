#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @Author      : Administrator
# @Email       : liaozhimingandy@qq.com
# @Date        : 2026/6/3 08:57
# @FileName    : service.py
# @Description : 以后台服务形式运行引擎
# @Project     : AlphaQuant
# @Copyright   : Copyright (c) 2026 Administrator, All Rights Reserved.
# -------------------------------------------------------------------------------
import os
import platform
import subprocess
from typing import Optional, Any, Generator

import psutil
from twisted.application import service

import signal
import sys
import time
from pathlib import Path

import click
from twisted.internet import reactor, defer

from app.core.engine import BaseQuantEngine
from app.core.engine.settings import EngineContext
from app.utils.logger import logger


# ======================【配置化：统一管理，无硬编码】======================
class ServiceConfig:
    SERVICE_NAME: str = "AlphaQuantEngine"
    BASE_DIR: Path = Path(__file__).parent
    VERSION: str = "0.0.1"
    PID_FILE: Path = BASE_DIR / "alphaquant.pid"
    LOG_FILE: Path = BASE_DIR / "alphaquant.log"
    GRACEFUL_STOP_TIMEOUT: int = 10  # 优雅退出超时10秒
    # 关闭twistd默认日志，避免冲突
    TWISTD_NO_LOG: bool = True


# ======================【引擎服务：严格遵循Twisted Service规范】======================
class EngineService(service.Service):
    def __init__(self):
        self.engine: Optional[BaseQuantEngine] = None
        self._is_stopping: bool = False
        self.CONFIG = {
            "RUN_MODE": "BACKTEST",  # 改BACKTEST就是回测自动停止
            "SYMBOL": "000001.SZ",
            "LOG_LEVEL": "INFO",
        }

    @defer.inlineCallbacks
    def startService(self) -> Generator[EngineContext, Any, None]:
        """【标准启动】Twisted 服务官方入口"""
        super().startService()
        logger.info("=" * 60)
        logger.info(f"🚀 启动 {ServiceConfig.SERVICE_NAME} 服务")
        logger.info("=" * 60)

        try:
            # 初始化引擎
            self.engine = BaseQuantEngine(self.CONFIG)
            # 注册安全信号（Twisted 官方方式，无线程冲突）
            # self._register_twisted_signals()
            # 启动引擎（异步，适配你的组件生命周期）
            yield self.engine.start()
            logger.info(f"✅ {ServiceConfig.SERVICE_NAME} 服务启动完成，运行中...")
        except Exception as e:
            logger.critical(f"❌ 服务启动失败: {str(e)}", exc_info=True)
            reactor.stop()

    @defer.inlineCallbacks
    def stopService(self) -> Generator[None, Any, None]:
        """【标准停止】优雅退出，超时保护"""
        if self._is_stopping or not self.engine:
            return
        self._is_stopping = True

        logger.info(f"🛑 开始优雅退出 (超时: {ServiceConfig.GRACEFUL_STOP_TIMEOUT}s)")
        try:
            # 调用你已有的引擎优雅停止
            yield defer.timeout(self.engine.stop(), ServiceConfig.GRACEFUL_STOP_TIMEOUT)
            logger.info("✅ 引擎优雅停止完成")
        except defer.TimeoutError:
            logger.error("⏰ 优雅退出超时，强制停止服务")
        except Exception as e:
            logger.error(f"❌ 停止服务异常: {str(e)}", exc_info=True)
        finally:
            super().stopService()
            reactor.stop()

    def _register_twisted_signals(self) -> None:
        """【Twisted 安全信号】替代原生signal，无阻塞、无冲突"""
        # SIGINT (Ctrl+C) + SIGTERM (kill命令) → 优雅退出
        reactor.addSystemEventTrigger("before", "shutdown", self.stopService)
        for sig in [signal.SIGINT, signal.SIGTERM]:
            reactor.addSignalHandler(sig, lambda _: self.stopService())

# ======================【进程管理：安全、自动清理、跨平台】======================
class ProcessManager:
    @staticmethod
    def _read_pid() -> Optional[int]:
        """读取PID文件，自动清理无效文件"""
        if not ServiceConfig.PID_FILE.exists():
            return None
        try:
            pid = int(ServiceConfig.PID_FILE.read_text().strip())
            return pid if psutil.pid_exists(pid) else None
        except Exception as e:
            ServiceConfig.PID_FILE.unlink(missing_ok=True)
            return None

    @classmethod
    def is_running(cls) -> bool:
        """检查服务状态"""
        return cls._read_pid() is not None

    @classmethod
    def get_status(cls) -> str:
        """获取状态字符串"""
        return "运行中" if cls.is_running() else "未运行"

    @classmethod
    def stop_process(cls) -> None:
        """优雅停止进程，超时强制关闭"""
        pid = cls._read_pid()
        if not pid:
            click.echo("❌ 服务未运行")
            return

        click.echo(f"✅ 发送停止信号到进程: {pid}")
        try:
            os.kill(pid, signal.SIGTERM)
            # 等待进程退出
            for _ in range(20):
                if not cls.is_running():
                    click.echo("✅ 服务已优雅停止")
                    return
                time.sleep(0.5)
            click.echo("⚠️ 超时未退出，强制终止进程")
            os.kill(pid, signal.SIGKILL)
        except Exception as e:
            click.echo(f"❌ 停止失败: {str(e)}")

# ====================== 核心 CLI 入口（完善版） ======================
@click.group(
    name="quant",
    help=click.style(
        f"🚀 {ServiceConfig.SERVICE_NAME} 量化引擎 - 生产级后台服务管理工具\n"
        f"支持后台守护、优雅退出、状态查询、Docker 部署",
        fg="cyan", bold=True
    ),
    context_settings={"help_option_names": ["-h", "--help"]}  # 支持 -h 快捷帮助
)
@click.version_option(
    version=ServiceConfig.VERSION,
    prog_name=ServiceConfig.SERVICE_NAME,
    message=click.style("%(prog)s %(version)s", fg="green", bold=True),
    help="查看引擎版本号"
)
def cli():
    """量化引擎主命令入口"""
    pass

# ====================== 1. 启动后台服务 ======================
@cli.command(
    name="start",
    help=click.style("启动量化引擎后台守护进程", fg="green"),
    short_help="启动后台服务"
)

def start():
    # 检查服务是否已运行
    if ProcessManager.is_running():
        click.secho(f"❌ 服务已运行中 | PID: {ProcessManager._read_pid()}", fg="yellow", bold=True)
        return

    # ==============================================
    # 🔥 核心：Windows 直接前台运行（最稳定，无任何报错）
    # ==============================================
    if platform.system() == "Windows":
        click.secho("ℹ️ Windows 环境将【前台运行】服务（开发推荐，无后台崩溃）", fg="cyan")
        click.secho("✅ 如需后台部署，请使用 Linux/Docker 环境", fg="green")
        run()
        return

    # ==============================================
    # Linux/Mac 标准后台运行（生产环境）
    # ==============================================
    click.secho(f"🚀 启动 {ServiceConfig.SERVICE_NAME} 后台服务...", fg="green")
    with open("/dev/null", "w") as devnull:
        subprocess.Popen(
            [sys.executable, __file__, "run"],
            stdout=devnull,
            stderr=devnull,
            preexec_fn=os.setsid
        )
    time.sleep(1)
    click.secho("✅ 后台服务启动成功！", fg="green")

# ====================== 2. 停止后台服务 ======================
@cli.command(
    name="stop",
    help=click.style("优雅停止量化引擎后台服务（安全退出，不丢失数据）", fg="yellow"),
    short_help="停止后台服务"
)
def stop():
    ProcessManager.stop_process()

# ====================== 3. 查看服务状态 ======================
@cli.command(
    name="status",
    help=click.style("查看引擎当前运行状态、进程ID、日志路径", fg="cyan"),
    short_help="查看服务状态"
)
def status():
    is_running = ProcessManager.is_running()
    if is_running:
        pid = ProcessManager._read_pid()
        click.secho(f"✅ 服务状态: 运行中", fg="green", bold=True)
        click.secho(f"🔖 进程PID: {pid}", fg="blue")
        click.secho(f"📂 日志路径: {ServiceConfig.LOG_FILE}", fg="blue")
        click.secho(f"⚙️  服务名称: {ServiceConfig.SERVICE_NAME}", fg="blue")
    else:
        click.secho(f"❌ 服务状态: 未运行", fg="red", bold=True)

# ====================== 4. 重启服务 ======================
@cli.command(
    name="restart",
    help=click.style("重启后台服务（先优雅停止，再自动启动）", fg="magenta"),
    short_help="重启后台服务"
)
@click.option("--wait", "-w", default=1, help="重启等待时间(秒)", type=int)
def restart(wait: int):
    click.secho(f"🔄 正在重启 {ServiceConfig.SERVICE_NAME} 服务...", fg="magenta", bold=True)
    stop()
    click.secho(f"⏳ 等待 {wait} 秒后启动...", fg="yellow")
    time.sleep(wait)
    start()

# ====================== 5. Docker 前台运行（核心新增） ======================
@cli.command(
    name="run",
    help=click.style("【Docker/调试专用】前台运行引擎（不后台守护，控制台实时输出日志）", fg="green"),
    short_help="前台运行引擎"
)
def run():
    if ProcessManager.is_running():
        click.secho("❌ 引擎已在后台运行，请勿重复启动！", fg="red", bold=True)
        sys.exit(1)

    click.secho(f"🚀 前台启动 {ServiceConfig.SERVICE_NAME} 引擎...", fg="green", bold=True)
    click.secho(f"⚠️  退出方式: Ctrl+C", fg="yellow")

    # 创建并启动服务
    service = EngineService()
    try:
        service.startService()
        reactor.run()  # 启动 Twisted 主循环
    except KeyboardInterrupt:
        click.secho("\n🛑 收到退出信号，正在优雅停止引擎...", fg="yellow", bold=True)
        service.stopService()
    except Exception as e:
        click.secho(f"❌ 引擎运行异常: {str(e)}", fg="red", bold=True)
        sys.exit(1)

# ====================== 6. 清空PID文件（故障修复） ======================
@cli.command(
    name="clean",
    help=click.style("清理异常退出残留的PID文件（修复服务无法启动问题）", fg="blue"),
    short_help="清理PID文件"
)
def clean():
    if ServiceConfig.PID_FILE.exists():
        ServiceConfig.PID_FILE.unlink()
        click.secho(f"✅ 已清理PID文件: {ServiceConfig.PID_FILE}", fg="green")
    else:
        click.secho(f"✅ 无需清理，PID文件不存在", fg="yellow")

# ======================【主入口】======================
if __name__ == "__main__":
    cli()