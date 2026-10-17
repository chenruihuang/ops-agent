"""知识源聚合出口。

data/source/ 是纯数据包：只放事实，不含渲染与 IO 逻辑。
文档组装与校验逻辑在 scripts/corpus_build.py。

分组归属：
    policies_arch.py : ALERTS / POLICIES / ARCH
    operations.py    : ONCALL / OBSERVABILITY / COMMANDS / FAQ
    services.py      : SERVICES / ERROR_CODES
    runbooks.py      : RUNBOOKS
    longform.py      : LONGFORM_DOCS（长文档，用于压测切块器的深层/超长路径）
"""

from __future__ import annotations

from data.source.operations import COMMANDS, FAQ, OBSERVABILITY, ONCALL
from data.source.policies_arch import ALERTS, ARCH, POLICIES
from data.source.runbooks import RUNBOOKS
from data.source.services import ERROR_CODES, SERVICES

# longform 依赖 services（容量基线表要从服务手册派生副本数，见该文件说明），
# 因此放在最后导入，避免在包初始化中途触发子模块加载。
from data.source.longform import LONGFORM_DOCS

__all__ = [
    "SERVICES",
    "ERROR_CODES",
    "RUNBOOKS",
    "ALERTS",
    "POLICIES",
    "ARCH",
    "ONCALL",
    "OBSERVABILITY",
    "COMMANDS",
    "FAQ",
    "LONGFORM_DOCS",
]
