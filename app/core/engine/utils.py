#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=================================================
    @Project: AlphaQuant
    @File： utils.py
    @Author：liaozhimingandy
    @Email: liaozhimingandy@gmail.com
    @Date：2026/6/2 20:16
    @Desc: 
=================================================
"""
from twisted.internet import defer, reactor


def async_sleep(seconds: float ) -> defer.Deferred:
    """
    全版本兼容的Twisted异步sleep，替代高版本才有的defer.sleep
    :param seconds: 等待时间（秒），支持小数如0.1、0.01
    :return: Deferred对象，等待完成后自动触发callback
    """
    d = defer.Deferred()
    reactor.callLater(delay=seconds, callable=d.callback, result=None)
    return d