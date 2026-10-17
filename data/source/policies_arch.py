"""其余运维文档知识源（告警 / 规范 / 架构 / 值班 / 可观测 / 命令 / FAQ）。

统一结构，便于渲染器一致处理：
    dict(key, title, category, tags, summary, sections=[(heading, body), ...], related=[doc_id])

其中 related 里引用的 doc_id 会被 scripts/check_corpus.py 校验存在性，
所以这里可以放心做交叉引用 —— 多跳检索 QA 就靠这些引用。
"""

from __future__ import annotations

# =====================================================================
# 一、告警规则（15 条）
# =====================================================================

ALERTS: list[dict] = [
    dict(
        key="alert-p0-gateway-5xx",
        title="网关 5xx 比例过高（P0）",
        category="alert",
        severity="P0",
        tags=["gateway", "可用性"],
        summary="网关 5xx 比例超过 5% 持续 1 分钟，判定为入口级故障，直接触发电话告警。",
        sections=[
            ("告警表达式", "```promql\nsum(rate(nginx_http_requests_total{status=~\"5..\"}[1m]))\n  / sum(rate(nginx_http_requests_total[1m])) > 0.05\n```"),
            ("触发条件", "比例 > 5%，持续 1m（`for: 1m`）"),
            ("通知渠道", "电话 + 企微 P0 群（@infra-oncall 直接呼人）"),
            ("处置入口", "见 rb-gateway-5xx；先区分「全部上游」还是「单一上游」"),
            ("常见误报", "发布窗口内的滚动重启会产生短时 5xx，已在 pol-gray-release 中约定发布期抑制窗口 3 分钟"),
        ],
        related=["svc-gateway", "rb-gateway-5xx"],
    ),
    dict(
        key="alert-p0-cache-hit",
        title="缓存命中率骤降（P0）",
        category="alert",
        severity="P0",
        tags=["cache", "redis"],
        summary="缓存命中率低于 50% 持续 2 分钟，通常意味着缓存雪崩或穿透，DB 即将被打爆。",
        sections=[
            ("告警表达式", "```promql\nsum(rate(cache_hits_total[2m]))\n  / sum(rate(cache_requests_total[2m])) < 0.5\n```"),
            ("触发条件", "命中率 < 50%，持续 2m"),
            ("通知渠道", "企微 P0 群 + 电话（夜间）"),
            ("处置入口", "见 rb-cache-avalanche（集中过期）或 rb-cache-penetration（查不存在 key）"),
            ("关联指标", "同时观察 `db_query_qps`：若 DB QPS 同步飙升即确认穿透"),
        ],
        related=["rb-cache-avalanche", "rb-cache-penetration"],
    ),
    dict(
        key="alert-p0-es-red",
        title="ES 集群状态 red（P0）",
        category="alert",
        severity="P0",
        tags=["search", "elasticsearch"],
        summary="ES 集群存在未分配的主分片，搜索功能不完整，需要立即介入。",
        sections=[
            ("告警表达式", "```promql\nelasticsearch_cluster_health_status{color=\"red\"} == 1\n```"),
            ("触发条件", "立即触发（`for: 0m`）"),
            ("通知渠道", "电话 + 企微 P0 群"),
            ("处置入口", "见 rb-es-cluster-red；第一步永远是 `_cluster/allocation/explain`"),
            ("区分", "yellow 状态（仅副本未分配）可容忍读正常，属 P1 而非 P0，处置同样走 rb-es-cluster-red"),
        ],
        related=["svc-search-service", "rb-es-cluster-red"],
    ),
    dict(
        key="alert-p0-link-timeout",
        title="全链路 P99 超时（P0）",
        category="alert",
        severity="P0",
        tags=["全链路", "雪崩"],
        summary="核心链路端到端 P99 超过 5s，说明已进入雪崩前兆，需立即断链止血。",
        sections=[
            ("告警表达式", "```promql\nhistogram_quantile(0.99,\n  sum(rate(link_duration_seconds_bucket{link=\"create_order\"}[5m])) by (le)\n) > 5\n```"),
            ("触发条件", "P99 > 5s，持续 2m"),
            ("通知渠道", "电话 + 企微 P0 群"),
            ("处置入口", "见 rb-full-link-timeout：先限流止血，再按 P99 恶化时间定位根因服务"),
            ("关键动作", "禁止直接重启服务，先切断级联传播路径"),
        ],
        related=["rb-full-link-timeout", "rb-threadpool-exhaust"],
    ),
    dict(
        key="alert-p0-pay-callback",
        title="支付回调积压（P0）",
        category="alert",
        severity="P0",
        tags=["payment", "资金"],
        summary="pay-callback-topic 消费积压超过 1000 条，订单状态可能出现不一致，涉及资金必须立即处理。",
        sections=[
            ("告警表达式", "```promql\nkafka_consumergroup_lag{group=\"pay-callback-consumer\"} > 1000\n```"),
            ("触发条件", "lag > 1000，持续 1m"),
            ("通知渠道", "电话 + 企微 P0 群 + 支付域负责人"),
            ("处置入口", "见 rb-pay-callback-timeout 与 rb-mq-backlog"),
            ("绝对禁止", "禁止重启支付服务（会打断处理中的资金流水）"),
        ],
        related=["svc-payment-gateway", "rb-pay-callback-timeout", "rb-mq-backlog"],
    ),
    dict(
        key="alert-p1-db-conn",
        title="MySQL 连接数过高（P1）",
        category="alert",
        severity="P1",
        tags=["mysql", "数据库"],
        summary="MySQL 活跃连接数超过 max_connections 的 85%，即将触发连接池耗尽。",
        sections=[
            ("告警表达式", "```promql\nmysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.85\n```"),
            ("触发条件", "> 85%，持续 3m"),
            ("通知渠道", "企微 P1 群"),
            ("处置入口", "见 rb-mysql-conn-pool"),
            ("先看什么", "`show processlist` 里 Sleep 多还是 Running 多 —— 前者是泄漏，后者是慢 SQL"),
        ],
        related=["rb-mysql-conn-pool", "rb-mysql-slow-query"],
    ),
    dict(
        key="alert-p1-slow-sql",
        title="慢查询数量异常（P1）",
        category="alert",
        severity="P1",
        tags=["mysql", "性能"],
        summary="每分钟新增执行时间超过 2s 的 SQL 数量超过 10 条，通常是发布引入的新查询。",
        sections=[
            ("告警表达式", "```promql\nincrease(mysql_slow_queries_total[1m]) > 10\n```"),
            ("触发条件", "> 10/min，持续 2m"),
            ("通知渠道", "企微 P1 群 + DBA 组"),
            ("处置入口", "见 rb-mysql-slow-query；用 pt-query-digest 按总耗时排序"),
            ("关联线索", "SQL 指纹数量突然增加 = 新版本发布引入"),
        ],
        related=["rb-mysql-slow-query", "rb-mysql-conn-pool"],
    ),
    dict(
        key="alert-p1-redis-mem",
        title="Redis 内存水位过高（P1）",
        category="alert",
        severity="P1",
        tags=["redis", "容量"],
        summary="Redis 内存使用率超过 maxmemory 的 85%，继续增长将触发 OOM 拒绝写入。",
        sections=[
            ("告警表达式", "```promql\nredis_memory_used_bytes / redis_memory_max_bytes > 0.85\n```"),
            ("触发条件", "> 85%，持续 5m"),
            ("通知渠道", "企微 P1 群"),
            ("处置入口", "见 rb-redis-oom；先查无 TTL 的 key 与 bigkey"),
            ("检查命令", "`redis-cli -c --cluster bigkeys` 与 `info keyspace`"),
        ],
        related=["rb-redis-oom", "rb-cache-avalanche"],
    ),
    dict(
        key="alert-p1-pod-restart",
        title="Pod 频繁重启（P1）",
        category="alert",
        severity="P1",
        tags=["k8s", "容器"],
        summary="同一 Deployment 下 Pod 在 10 分钟内重启超过 3 次，优先怀疑 OOMKilled。",
        sections=[
            ("告警表达式", "```promql\nincrease(kube_pod_container_status_restarts_total[10m]) > 3\n```"),
            ("触发条件", "> 3 次/10m"),
            ("通知渠道", "企微 P1 群 + 业务域值班"),
            ("处置入口", "见 rb-pod-oomkilled"),
            ("第一步", "`kubectl describe pod | grep -A3 'Last State'` 看 Reason 是否为 OOMKilled"),
        ],
        related=["rb-pod-oomkilled", "rb-cpu-high"],
    ),
    dict(
        key="alert-p1-threadpool",
        title="线程池队列水位过高（P1）",
        category="alert",
        severity="P1",
        tags=["java", "线程池"],
        summary="线程池队列使用率超过 80%，接近拒绝阈值（ORD-503-002），本质通常是下游变慢。",
        sections=[
            ("告警表达式", "```promql\nexecutor_queued / executor_queue_capacity > 0.8\n```"),
            ("触发条件", "> 80%，持续 2m"),
            ("通知渠道", "企微 P1 群"),
            ("处置入口", "见 rb-threadpool-exhaust"),
            ("关键认知", "调大队列不是解法，要先用 threaddump 找出线程卡在哪个外部调用上"),
        ],
        related=["rb-threadpool-exhaust", "rb-full-link-timeout"],
    ),
    dict(
        key="alert-p1-gw-conn",
        title="网关连接数过高（P1）",
        category="alert",
        severity="P1",
        tags=["gateway", "nginx"],
        summary="网关已建立连接数超过 worker_connections 的 80%，接近「worker_connections are not enough」。",
        sections=[
            ("告警表达式", "```promql\nnginx_connections_active / (nginx_worker_processes * 65535) > 0.8\n```"),
            ("触发条件", "> 80%，持续 3m"),
            ("通知渠道", "企微 P1 群"),
            ("处置入口", "见 rb-gateway-conn-exhaust"),
            ("优先动作", "开 upstream keepalive，而不是提高连接上限"),
        ],
        related=["svc-gateway", "rb-gateway-conn-exhaust"],
    ),
    dict(
        key="alert-p1-mq-lag",
        title="Kafka 消费积压（P1）",
        category="alert",
        severity="P1",
        tags=["kafka", "消息队列"],
        summary="任意消费组 lag 超过 10 万条，下游数据同步延迟。",
        sections=[
            ("告警表达式", "```promql\nkafka_consumergroup_lag > 100000\n```"),
            ("触发条件", "> 10 万，持续 5m"),
            ("通知渠道", "企微 P1 群"),
            ("处置入口", "见 rb-mq-backlog"),
            ("关键上限", "消费并行度上限 = 分区数，消费者超过分区数无效"),
        ],
        related=["rb-mq-backlog", "svc-search-service"],
    ),
    dict(
        key="alert-p1-disk-usage",
        title="磁盘使用率过高（P1）",
        category="alert",
        severity="P1",
        tags=["磁盘", "容量"],
        summary="任意挂载点使用率超过 85%，需要提前清理，不要等到写失败。",
        sections=[
            ("告警表达式", "```promql\n(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) > 0.85\n```"),
            ("触发条件", "> 85%，持续 5m"),
            ("通知渠道", "企微 P1 群"),
            ("处置入口", "见 rb-disk-full；同时用 `df -i` 排查 inode（rb-inode-exhaust）"),
            ("隐性杀手", "heap dump / core dump 文件常被忽略"),
        ],
        related=["rb-disk-full", "rb-inode-exhaust"],
    ),
    dict(
        key="alert-p1-risk-fp",
        title="风控误杀率异常（P1）",
        category="alert",
        severity="P1",
        tags=["风控", "业务"],
        summary="风控误杀率（正常用户被拦截比例）超过 1%，直接影响成交。",
        sections=[
            ("告警表达式", "```promql\nrisk_false_positive_total / risk_decision_total > 0.01\n```"),
            ("触发条件", "> 1%，持续 5m"),
            ("通知渠道", "企微 P1 群 + 风控域 + 交易域"),
            ("处置入口", "见 rb-risk-false-positive；规则支持热更新，60s 生效"),
            ("最快手段", "回滚规则版本，不需要发版"),
        ],
        related=["svc-risk-engine", "rb-risk-false-positive"],
    ),
    dict(
        key="alert-p2-cert-expire",
        title="证书临近过期（P2）",
        category="alert",
        severity="P2",
        tags=["证书", "巡检"],
        summary="域名证书剩余有效期少于 30 天，提前续签，避免演变为 P0（rb-cert-expired）。",
        sections=[
            ("告警表达式", "```promql\n(probe_ssl_earliest_cert_expiry - time()) / 86400 < 30\n```"),
            ("触发条件", "< 30 天，每日巡检触发一次"),
            ("通知渠道", "企微 P2 群 + 邮件"),
            ("处置入口", "见 rb-cert-expired；优先修复 cert-manager 自动续签"),
            ("教训", "所有 P0 证书过期事故，都是因为这条 P2 告警没人看"),
        ],
        related=["rb-cert-expired", "svc-gateway"],
    ),
]


# =====================================================================
# 二、规范与流程（7 篇）
# =====================================================================

POLICIES: list[dict] = [
    dict(
        key="pol-db-change",
        title="数据库变更流程规范",
        category="policy",
        tags=["DBA", "变更"],
        summary="所有 DDL（建表 / 加索引 / 改字段）必须走 DBA 变更流程，禁止应用侧直连执行 DDL。",
        sections=[
            ("适用范围", "所有生产库（order_db / user_db / payment_db / inventory_db / promo_db / risk_db / report_db）的 DDL 与大批量 DML。"),
            ("流程", "① 提交提单并附 `EXPLAIN` 结果与预估影响行数 → ② DBA 审核（重点看是否锁表）→ "
                     "③ 在只读从库先验证 → ④ 低峰期（22:00-06:00）在主库执行 → ⑤ 观察 30 分钟慢查询指标。"),
            ("禁止事项", "禁止在业务高峰期执行 DDL；禁止使用 `ALTER TABLE` 直接改大表字段类型（需用 gh-ost / pt-online-schema-change）；"
                       "禁止跳过 DBA 审核用应用账号执行 DDL。"),
            ("加索引要求", "新索引上线前必须提供 `EXPLAIN` 前后对比；单表索引数量不超过 6 个（写放大考虑）。"),
            ("回滚方式", "索引回滚 `DROP INDEX`；字段变更需准备反向 DDL，并在变更单中写明。"),
            ("关联", "慢查询引发的故障见 rb-mysql-slow-query；连接池问题见 rb-mysql-conn-pool。"),
        ],
        related=["rb-mysql-slow-query", "rb-mysql-conn-pool", "alert-p1-slow-sql"],
    ),
    dict(
        key="pol-config-change",
        title="配置变更流程规范",
        category="policy",
        tags=["配置", "变更"],
        summary="配置变更走 ConfigMap / Nacos，禁止直接改容器内文件；所有配置项必须有默认值与类型校验。",
        sections=[
            ("变更载体", "K8s ConfigMap（部署相关）与 Nacos（运行时参数）。应用侧启动时必须校验必填配置，缺失则快速失败退出。"),
            ("热更新范围", "限流阈值、线程池参数、超时时间、Redis `maxmemory-policy` 支持热更新；"
                        "数据库连接串、端口等需重启。"),
            ("生效与验证", "Nacos 变更后 60s 内全副本生效；变更后必须在 Grafana 观察 5 分钟确认指标无异常。"),
            ("回滚", "Nacos 保留历史版本，一键回退；ConfigMap 变更走 `kubectl rollout undo`。"),
            ("禁止", "禁止把密钥（数据库密码 / API Key）明文写入 ConfigMap，必须走 Secret 或密钥管理服务。"),
            ("关联", "Redis policy 变更的典型场景见 rb-redis-oom。"),
        ],
        related=["rb-redis-oom", "pol-emergency"],
    ),
    dict(
        key="pol-capacity-preheat",
        title="大促容量评估与缓存预热规范",
        category="policy",
        tags=["大促", "容量", "缓存"],
        summary="大促前必须完成容量评估与缓存预热，避免开闸瞬间缓存未命中引发雪崩。",
        sections=[
            ("时间线", "T-7 完成容量评估 → T-3 完成压测 → T-1 完成缓存预热 → T-0 开闸后 30 分钟内禁止发布。"),
            ("容量评估方法", "按预估峰值 QPS × 安全系数 1.5 计算所需副本数；单副本吞吐以压测基线为准（每档跑 2 分钟）。"),
            ("预热范围", "商品详情、库存、优惠券、用户基础信息。预热脚本按 SKU / user_id 分片并发写入，"
                      "**必须给 TTL 加 ±10% 抖动**，否则会制造下一次集中过期（见 rb-cache-avalanche）。"),
            ("预热验收", "预热后缓存命中率必须 ≥ 95%，未达标不允许开闸。"),
            ("压测要求", "阶梯加压 10→30→50→80→120 并发，取「错误率 < 1% 且 P95 < 3s」的最大档作为容量结论。"),
            ("关联", "缓存雪崩处置见 rb-cache-avalanche；容量与 SLA 定义见 arch-capacity、arch-sla。"),
        ],
        related=["rb-cache-avalanche", "arch-capacity", "arch-sla", "rb-cache-penetration"],
    ),
    dict(
        key="pol-security-incident",
        title="安全事件应急响应规范",
        category="policy",
        tags=["安全", "应急"],
        summary="发现疑似攻击、数据泄露、越权访问时，必须同时启动安全响应与技术处置两条线。",
        sections=[
            ("事件分级", "S1（数据泄露 / 越权访问生产数据）、S2（大规模攻击 / 爬虫）、S3（单点异常访问）。"),
            ("立即动作", "① 保留现场（不要清理日志）② 在网关封禁来源 IP ③ 通知安全组 ④ 涉及数据泄露立即上报合规。"),
            ("取证要求", "导出网关 access.log、应用日志、数据库审计日志；全程不做破坏性操作。"),
            ("缓存穿透型攻击", "高频请求不存在 ID 是最常见的爬虫特征，处置见 rb-cache-penetration。"),
            ("复盘要求", "S1 / S2 事件必须在 3 个工作日内完成复盘，模板见 oncall-postmortem。"),
            ("关联", "权限申请与最小权限原则见 faq-06、obs-log-spec。"),
        ],
        related=["rb-cache-penetration", "oncall-postmortem", "faq-06"],
    ),
    dict(
        key="pol-gray-release",
        title="灰度发布规范",
        category="policy",
        tags=["发布", "灰度"],
        summary="所有线上发布必须灰度，禁止全量直接替换；灰度比例与观察时长按风险等级确定。",
        sections=[
            ("灰度阶梯", "1% → 10% → 50% → 100%，每档观察 10 分钟。资金相关服务（payment-gateway）每档观察 30 分钟。"),
            ("观察指标", "P95 耗时、5xx 比例、错误码新增、CPU / 内存曲线。任一指标恶化超过 20% 立即暂停灰度。"),
            ("发布窗口", "默认禁止 20:00-22:00（流量高峰）与周五下午发布；大促锁定期见 pol-capacity-preheat。"),
            ("告警抑制", "发布期间对目标服务的 Pod 重启告警设置 3 分钟抑制窗口，避免误报（见 alert-p0-gateway-5xx 的误报说明）。"),
            ("回滚", "灰度异常必须回滚而不是「继续观察」，回滚命令与时限见 pol-rollback。"),
            ("关联", "紧急绕过灰度需走 pol-emergency。"),
        ],
        related=["pol-rollback", "pol-emergency", "alert-p0-gateway-5xx"],
    ),
    dict(
        key="pol-rollback",
        title="发布回滚规范",
        category="policy",
        tags=["发布", "回滚"],
        summary="回滚时限：P0 故障 5 分钟内必须完成回滚决策，禁止边排查边修复（先恢复再定位）。",
        sections=[
            ("回滚优先级", "第一优先级是恢复服务，不是定位根因。5 分钟内无法定位的 P0 故障，必须回滚上一版本。"),
            ("标准命令", "`kubectl rollout undo deploy/<name> -n <ns>`；回滚后 `kubectl rollout status` 确认全副本就绪。"),
            ("数据库兼容", "回滚前必须确认新版本没有执行不可回滚的 DDL。若已执行，需准备前向修复而不是回滚（见 pol-db-change）。"),
            ("缓存处理", "应用回滚后需评估缓存是否包含新版本产生的数据格式；必要时 bump 缓存版本号（kb_version 机制）。"),
            ("记录", "回滚操作必须记录到事故时间线（操作人 / 时间 / 命令 / 结果），用于复盘。"),
        ],
        related=["pol-db-change", "pol-gray-release", "oncall-postmortem"],
    ),
    dict(
        key="pol-emergency",
        title="紧急变更规范",
        category="policy",
        tags=["变更", "应急"],
        summary="故障处置需要绕过常规流程时走紧急变更，必须事后补单并说明原因。",
        sections=[
            ("适用场景", "P0 / P1 故障处置过程中，需要立即调整配置、扩容、摘除节点、热修规则。"),
            ("放行方式", "在企微 P0 群 @值班负责人 口头授权 + 记录操作时间，即可执行；不等待常规审批。"),
            ("允许的操作", "扩容副本、调限流阈值、调整超时、摘除实例、回滚规则版本、清理磁盘。"),
            ("禁止的操作", "手工改数据库数据（除支付对账订正，需财务确认）、删除生产命名空间、直接改 Redis key 内容。"),
            ("事后要求", "24 小时内补齐变更单，并在复盘会议中说明为何无法走常规流程。"),
            ("关联", "紧急发布与灰度流程的关系见 pol-gray-release。"),
        ],
        related=["pol-gray-release", "pol-rollback"],
    ),
]


# =====================================================================
# 三、架构与容量（7 篇）
# =====================================================================

ARCH: list[dict] = [
    dict(
        key="arch-overview",
        title="系统整体架构说明",
        category="arch",
        tags=["架构", "总览"],
        summary="交易系统采用微服务 + K8s 部署，入口 OpenResty 网关，共 12 个服务，缓存与消息中间件为核心依赖。",
        sections=[
            ("分层", "接入层（gateway）→ 业务层（user-center / order-service / payment-gateway / inventory-service / "
                     "promotion-service / search-service / risk-engine）→ 支撑层（notify-service / file-service / report-job / ops-agent）→ "
                     "数据层（MySQL / Redis Cluster / Kafka / ES / MinIO）。"),
            ("服务清单", "共 12 个服务，端口 8081-8090，全部部署在 K8s 的 5 个命名空间：infra / user / trade / pay / search / promo / data / risk。"),
            ("关键依赖", "Redis Cluster（缓存）、Kafka（异步解耦）、MySQL（持久化）、ES（搜索）、Nacos（服务发现）。"),
            ("数据流", "用户请求 → 网关鉴权限流 → 业务服务 → 缓存读 / DB 写 → Kafka 事件 → 下游异步处理（索引 / 通知 / 报表）。"),
            ("设计原则", "无状态服务（会话外置 Redis）；跨服务调用必须带超时与熔断；异步优先。"),
            ("关联", "容量与 SLA 见 arch-capacity、arch-sla；容灾见 arch-multi-az。"),
        ],
        related=["arch-capacity", "arch-sla", "arch-multi-az", "svc-gateway"],
    ),
    dict(
        key="arch-es-reindex",
        title="ES 索引重建与别名切换",
        category="arch",
        tags=["elasticsearch", "搜索"],
        summary="索引结构变更走「新建索引 → 双写 → 全量回填 → 别名切换」四步，不走原地修改。",
        sections=[
            ("为什么重建", "ES 不支持修改已有字段类型，映射变更必须重建索引。"),
            ("四步流程", "① 新建 product_v8 索引（含新映射）→ ② 应用双写到 v7 与 v8 → "
                       "③ 全量回填历史数据并校验 doc 数一致 → ④ 原子切换别名 product 指向 v8。"),
            ("别名切换命令", "```json\nPOST /_aliases\n{\"actions\":[{\"remove\":{\"index\":\"product_v7\",\"alias\":\"product\"}},\n"
                          "{\"add\":{\"index\":\"product_v8\",\"alias\":\"product\"}}]}\n```"),
            ("回滚", "把别名的 remove / add 顺序对调即可指回 v7，**这是搜索域最重要的回滚手段**。"),
            ("注意事项", "切换前必须确认 doc 数一致（`_count` 对比），不一致时回填会丢数据；切换后观察 30 分钟再删旧索引。"),
            ("关联", "集群异常时的处置见 rb-es-cluster-red；磁盘不足会阻塞分片分配，见 rb-disk-full。"),
        ],
        related=["svc-search-service", "rb-es-cluster-red", "rb-disk-full"],
    ),
    dict(
        key="arch-traffic-model",
        title="流量模型与峰值特征",
        category="arch",
        tags=["容量", "流量"],
        summary="日常峰值约 8000 QPS，大促峰值约 45000 QPS，流量呈明显的早晚双峰与整点脉冲特征。",
        sections=[
            ("日常模型", "日间峰值 8000 QPS（12:00 与 20:00 双峰），夜间谷值 500 QPS。"),
            ("大促模型", "预计峰值 45000 QPS，是日常峰值的 5.6 倍；开闸瞬间 30 秒内流量陡增，是最危险的时间窗。"),
            ("脉冲特征", "整点秒杀会在 1 秒内产生日常 20 倍的瞬时流量，因此限流必须按秒级窗口而非分钟级。"),
            ("热点分布", "80% 流量集中在 5% 的热点 SKU 上，因此库存服务必须处理热点分片问题（见 svc-inventory-service）。"),
            ("容量换算", "按单副本压测基线推算：order-service 单副本支撑 300 QPS，大促需 45000/300×1.5≈225 副本 → "
                       "实际通过限流 + 缓存把 RAG 之外的流量压到 HPA 上限（16 副本）可承载范围内。"),
            ("关联", "预热与压测要求见 pol-capacity-preheat；容量结论记录见 arch-capacity。"),
        ],
        related=["pol-capacity-preheat", "arch-capacity", "svc-inventory-service"],
    ),
    dict(
        key="arch-capacity",
        title="容量规划方法",
        category="arch",
        tags=["容量"],
        summary="容量结论必须来自压测，禁止拍脑袋；所有容量数字都要标注测量条件（副本数、并发、数据集）。",
        sections=[
            ("计算方法", "所需副本数 = 预估峰值 QPS × 安全系数 1.5 ÷ 单副本吞吐基线。单副本基线由压测得出。"),
            ("压测方法", "阶梯加压 10 → 30 → 50 → 80 → 120 并发，每档 2 分钟；取「错误率 < 1% 且 P95 < 3s」的最大档。"),
            ("关键结论", "数据库连接池上限 × 副本数 必须小于 MySQL `max_connections`，这是最容易被忽略的容量约束。"),
            ("横向扩展验证", "1 副本与 3 副本对比吞吐，验证无状态化是否有效（会话状态是否真的外置了）。"),
            ("资源配额", "CPU request 与 limit 比值不低于 0.5，避免被 CFS 限流；内存 limit 要留足堆外空间（见 rb-pod-oomkilled）。"),
            ("关联", "压测与容量评估的流程要求见 pol-capacity-preheat。"),
        ],
        related=["pol-capacity-preheat", "arch-traffic-model", "rb-pod-oomkilled", "arch-circuit-breaker"],
    ),
    dict(
        key="arch-circuit-breaker",
        title="限流熔断与降级策略",
        category="arch",
        tags=["高可用", "熔断"],
        summary="所有跨服务与跨系统调用必须配置超时 + 熔断 + 有限重试 + 降级，缺一不可。",
        sections=[
            ("超时基线", "连接 1s / 读 3s（网关到上游）；外部渠道连接 1s / 读 3s；风控名单查询 50ms。"),
            ("熔断参数", "失败率 > 30% 持续 10s 触发熔断，熔断时长 60s，半开状态放行 5 个请求探活。"),
            ("重试策略", "最多重试 1 次，仅对幂等接口；重试必须带退避（1s / 5s / 30s 指数退避）。"),
            ("降级链示例", "支付渠道熔断 → 降级为「排队中」异步受理；向量库不可用 → 跳过召回直接作答并标注无依据；"
                       "LLM 限流 → 切换备用模型 → 全部失败则返回模板应答。"),
            ("风控的特殊选择", "风控采用 fail-open（超时即放行），宁可放过不可误杀，见 svc-risk-engine。"),
            ("关联", "雪崩处置见 rb-full-link-timeout；线程池隔离见 rb-threadpool-exhaust。"),
        ],
        related=["rb-full-link-timeout", "rb-threadpool-exhaust", "svc-risk-engine"],
    ),
    dict(
        key="arch-multi-az",
        title="多可用区容灾设计",
        category="arch",
        tags=["容灾", "高可用"],
        summary="服务同城双 AZ 部署，AZ 级故障容忍；数据层主从跨 AZ，切换目标 RTO 5 分钟 / RPO 0。",
        sections=[
            ("部署模型", "每个服务副本跨 2 个可用区打散（`topologySpreadConstraints`），单 AZ 故障损失不超过 50% 副本。"),
            ("数据层", "MySQL 一主两从（从库分布在另外两个 AZ）；Redis Cluster 主从分片跨 AZ；Kafka 分区副本数 3。"),
            ("切换目标", "AZ 故障 RTO ≤ 5 分钟，RPO = 0（依赖同步复制）。"),
            ("演练", "每季度一次单 AZ 故障演练，演练流程见 oncall-drill；节点级故障处置见 rb-node-notready。"),
            ("风险点", "跨 AZ 网络延迟高于同 AZ，主从同步延迟监控指标为 `mysql_slave_lag_seconds`，超过 3s 告警。"),
        ],
        related=["oncall-drill", "rb-node-notready", "arch-sla"],
    ),
    dict(
        key="arch-sla",
        title="SLA 与 SLO 定义",
        category="arch",
        tags=["SLA", "指标"],
        summary="SLO 按业务重要度分级：支付与风控 99.99%，交易与用户 99.9%-99.95%，离线任务 99.5%。",
        sections=[
            ("分级标准", "99.99%（支付 / 风控，在资金关键路径）、99.95%（订单 / 库存 / 网关）、99.9%（用户 / 营销 / 搜索 / 文件）、99.5%（报表离线）。"),
            ("延迟目标", "风控 P95 < 100ms；库存 P95 < 150ms；用户 P95 < 200ms；网关 P95 < 300ms；订单 P95 < 400ms；支付 P95 < 800ms。"),
            ("错误预算", "按 99.95% 计算，月度不可用时间上限约 21.6 分钟；错误预算耗尽后冻结非必要发布。"),
            ("可用性测量方式", "以「1 分钟粒度的成功率 < 99% 记为不可用」聚合，**不是**简单的请求成功率平均值。"),
            ("单机测试的诚实边界", "本地单机环境无法测量生产 SLA，只能测量「故障注入下的请求成功率」。"
                            "对外表述必须注明测量条件，禁止把实验值写成生产 SLA。"),
            ("关联", "容量与压测见 arch-capacity；告警阈值见 ALERTS 系列文档。"),
        ],
        related=["arch-capacity", "arch-circuit-breaker", "alert-p0-link-timeout"],
    ),
]
