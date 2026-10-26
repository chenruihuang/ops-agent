"""生成 40 条评估标注集（eval/questions.json）+ 工具调用 mock 数据。

设计原则
--------
1. **与语料同源**：golden_doc_ids 在写入前逐一对照 data/raw/MANIFEST.json，
   任何悬空引用直接报错退出 —— 标注悬空在源头被杜绝。
2. **五维检索覆盖**（notes 里标注 qtype）：
   single-hop（单跳事实）/ multi-hop（跨文档推理）/ colloquial（口语化改写，
   考验 query rewrite）/ edge（边界与否定）/ multimodal（与截图对应）。
3. **四类意图覆盖**：rag / tool / hybrid / chitchat —— 意图路由本身也是被测能力。
4. **难度分层**：easy / medium / hard，避免只考简单题导致指标虚高（见 obs-sampling）。

用法：
    python -m scripts.gen_eval
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "data" / "raw" / "MANIFEST.json"
OUT_QUESTIONS = ROOT / "eval" / "questions.json"
OUT_MOCK = ROOT / "eval" / "mock_records.json"

# D2 Java MCP Server 计划提供的工具（与 faq-07 / svc-ops-agent 对齐）
KNOWN_TOOLS = {
    "query_ticket_by_no",
    "query_ticket_by_keyword",
    "query_ticket_by_service",
    "get_asset_by_code",
    "query_alerts_by_service",
}

# ---------------------------------------------------------------------
# 工具调用 mock 数据（D2 导入 MySQL；tool/hybrid 题的工单号从这里取）
# ---------------------------------------------------------------------

MOCK_RECORDS: dict = {
    "tickets": [
        {"ticket_no": "INC-2026-0412", "title": "订单服务容器 OOMKilled 反复重启", "service": "order-service",
         "status": "处理中", "owner": "@trade-oncall", "related_runbook": "rb-pod-oomkilled",
         "summary": "heap dump 显示 LocalSkuCache 持有 3.1GB 大对象，确认内存泄漏，修复中。"},
        {"ticket_no": "INC-2026-0389", "title": "支付回调积压 1428 条", "service": "payment-gateway",
         "status": "已解决", "owner": "@pay-oncall", "related_runbook": "rb-pay-callback-timeout",
         "summary": "扩容消费者后积压清零，217 笔超时订单通过对账兜底完成状态修正。"},
        {"ticket_no": "INC-2026-0401", "title": "网关 TLS 证书过期导致全站 HTTPS 不可访问", "service": "gateway",
         "status": "已解决", "owner": "@infra-oncall", "related_runbook": "rb-cert-expired",
         "summary": "cert-manager ACME DNS-01 校验超时导致自动续签失败，已手工换证并修复续签。"},
        {"ticket_no": "INC-2026-0415", "title": "order_db 慢查询导致连接池耗尽", "service": "order-service",
         "status": "已关闭", "owner": "@trade-oncall", "related_runbook": "rb-mysql-slow-query",
         "summary": "order_main 缺 user_phone 索引引发全表扫描，补索引后 P95 由 6.8s 回落至 210ms。"},
        {"ticket_no": "INC-2026-0420", "title": "用户重复收到短信", "service": "notify-service",
         "status": "处理中", "owner": "@infra-oncall", "related_runbook": "",
         "summary": "渠道重试与频控计数 key 过期窗口重叠，正在调整频控 TTL。"},
        {"ticket_no": "INC-2026-0433", "title": "ES 集群 red，搜索结果不完整", "service": "search-service",
         "status": "已解决", "owner": "@search-oncall", "related_runbook": "rb-es-cluster-red",
         "summary": "app-log 索引写入打满 data 节点磁盘（>85% 水位拒绝分配），清理日志后恢复。"},
    ],
    "assets": [
        {"asset_code": "SRV-0088", "hostname": "app-prod-01", "ip": "10.0.1.11", "status": "online",
         "room": "A 区-机柜 3", "services": ["order-service", "user-center"]},
        {"asset_code": "SRV-0077", "hostname": "cache-prod-02", "ip": "10.0.3.22", "status": "degraded",
         "room": "A 区-机柜 5", "services": ["redis-cluster"]},
        {"asset_code": "SRV-0102", "hostname": "db-prod-03", "ip": "10.0.2.13", "status": "online",
         "room": "B 区-机柜 1", "services": ["order_db-slave"]},
    ],
    # 告警：query_alerts_by_service 的数据源。
    #
    # 这份数据是**刻意设计成有区分度**的，不是随便凑几条：
    #   1. order-service 有 1 条 firing → 验证"能查出未关闭告警"
    #   2. search-service 有 1 条 resolved → 验证"已解决的不能被算成未关闭"
    #   3. inventory-service **只有 resolved** → 标注 q-36 期望的正是
    #      "inventory-service 当前无未关闭告警"，即考察**工具查不到时必须说「无」
    #      而不是编造**。这是工具类题最容易失败的地方。
    # 如果这里给 inventory-service 塞一条 firing，q-36 的标注就自相矛盾了。
    "alerts": [
        {"alert_name": "OrderServiceHttp5xxRate", "service": "order-service", "severity": "P1",
         "status": "firing", "started_at": "2026-10-16 09:12", "summary": "订单服务 5xx 比例 2.8%，超过 1% 阈值，持续 12 分钟"},
        {"alert_name": "PaymentCallbackBacklog", "service": "payment-gateway", "severity": "P1",
         "status": "firing", "started_at": "2026-10-16 08:40", "summary": "支付回调积压 1428 条，超过 500 阈值"},
        {"alert_name": "SearchClusterRed", "service": "search-service", "severity": "P0",
         "status": "resolved", "started_at": "2026-10-15 22:05", "ended_at": "2026-10-15 23:41",
         "summary": "ES 集群 red，日志索引打满磁盘；清理后恢复"},
        {"alert_name": "InventoryNodeDiskUsage", "service": "inventory-service", "severity": "P2",
         "status": "resolved", "started_at": "2026-10-14 14:20", "ended_at": "2026-10-14 15:02",
         "summary": "库存服务节点磁盘使用率 82%，已清理归档日志"},
    ],
}

# ---------------------------------------------------------------------
# 40 条标注
# 字段：qtype 仅存于 notes 前缀，用于后续按维度分析召回率
# ---------------------------------------------------------------------

Q: list[dict] = [
    # ---------------- 单跳事实（12）----------------
    dict(id=1, intent="rag", difficulty="easy", qtype="single-hop",
         question="订单服务对单用户的下单限流是多少？超限返回什么错误码？",
         golden_doc_ids=["svc-order-service", "err-order-service"],
         answer_anchors=["QPS", "5", "ORD-429-001"],
         key_points=["单用户下单 QPS 限 5", "超限返回 ORD-429-001", "提示用户稍后重试"]),
    dict(id=2, intent="rag", difficulty="easy", qtype="single-hop",
         question="错误码 GW-504-001 代表什么含义？",
         golden_doc_ids=["err-gateway", "svc-gateway"],
         answer_anchors=["上游读超时", "3s"],
         key_points=["网关上游读超时", "读超时阈值为 3s", "幂等接口可重试"]),
    dict(id=3, intent="rag", difficulty="medium", qtype="single-hop",
         question="订单服务的 JVM 堆内存和容器内存限制分别是多少？两者有什么配比要求？",
         golden_doc_ids=["svc-order-service", "rb-pod-oomkilled"],
         answer_anchors=["-Xmx4g", "5Gi", "75%"],
         key_points=["-Xms2g -Xmx4g", "容器内存 limit 5Gi", "堆应设为 limit 的 75% 以内，给堆外留空间"]),
    dict(id=4, intent="rag", difficulty="easy", qtype="single-hop",
         question="支付网关对渠道的熔断参数是什么？熔断期间怎么处理用户请求？",
         golden_doc_ids=["svc-payment-gateway", "arch-circuit-breaker"],
         answer_anchors=["30%", "60s", "排队中"],
         key_points=["单渠道失败率 > 30% 触发熔断 60s", "熔断期间降级为「排队中」异步受理", "不直接报错"]),
    dict(id=5, intent="rag", difficulty="easy", qtype="single-hop",
         question="缓存命中率低于多少会触发 P0 告警？需要持续多久？",
         golden_doc_ids=["alert-p0-cache-hit"],
         answer_anchors=["50%", "2 分钟"],
         key_points=["命中率 < 50%", "持续 2 分钟触发", "通常意味着雪崩或穿透"]),
    dict(id=6, intent="rag", difficulty="easy", qtype="single-hop",
         question="域名证书剩余有效期少于多少天会触发告警？",
         golden_doc_ids=["alert-p2-cert-expire", "rb-cert-expired"],
         answer_anchors=["30 天", "每日巡检"],
         key_points=["< 30 天触发 P2 告警", "每日巡检触发一次", "优先修复 cert-manager 自动续签"]),
    dict(id=7, intent="rag", difficulty="easy", qtype="single-hop",
         question="灰度发布的流量阶梯和每档观察时长是怎么规定的？",
         golden_doc_ids=["pol-gray-release"],
         answer_anchors=["1%", "10%", "50%", "100%", "10 分钟"],
         key_points=["1% → 10% → 50% → 100%", "每档观察 10 分钟", "支付服务每档 30 分钟"]),
    dict(id=8, intent="rag", difficulty="easy", qtype="single-hop",
         question="生产数据库的 DDL 变更允许在什么时间窗口执行？",
         golden_doc_ids=["pol-db-change"],
         answer_anchors=["22:00", "06:00", "DBA 审核"],
         key_points=["低峰期 22:00-06:00", "必须先过 DBA 审核", "禁止高峰期执行 DDL"]),
    dict(id=9, intent="rag", difficulty="medium", qtype="single-hop",
         question="MySQL 出现死锁后，代码层面应该按什么规范修复？",
         golden_doc_ids=["rb-mysql-deadlock"],
         answer_anchors=["更新顺序", "主键升序", "短事务"],
         key_points=["统一按主键升序更新", "缩小事务粒度避免远程调用", "锁范围扩大时补索引"]),
    dict(id=10, intent="rag", difficulty="easy", qtype="single-hop",
         question="生产环境 Redis 的 maxmemory-policy 应该配置成什么？",
         golden_doc_ids=["rb-redis-oom"],
         answer_anchors=["allkeys-lru", "noeviction"],
         key_points=["应为 allkeys-lru", "noeviction 会导致写入被拒（OOM）", "policy 变更走 pol-config-change"]),
    dict(id=11, intent="rag", difficulty="medium", qtype="single-hop",
         question="事故复盘里 MTTR 是怎么定义的？由哪些时间戳计算？",
         golden_doc_ids=["oncall-postmortem"],
         answer_anchors=["T5", "T2", "恢复时间"],
         key_points=["MTTR = T5 - T2（故障发生到完全恢复）", "MTTD = T2 - T1（检测时间）", "时间线以分钟为粒度"]),
    dict(id=12, intent="rag", difficulty="easy", qtype="single-hop",
         question="ops-agent 对用户的首 token 延迟（TTFT）SLO 是多少？缓存命中时呢？",
         golden_doc_ids=["svc-ops-agent"],
         answer_anchors=["1.5s", "50ms"],
         key_points=["TTFT P95 < 1.5s", "缓存命中时 TTFT < 50ms 直接返回", "不走 LLM"]),

    # ---------------- 多跳跨文档（7）----------------
    dict(id=13, intent="rag", difficulty="hard", qtype="multi-hop",
         question="订单服务容器 OOMKilled 反复重启，除了内存问题本身，还会连锁引发什么问题？接下来应该按哪些文档处理？",
         golden_doc_ids=["rb-pod-oomkilled", "rb-threadpool-exhaust"],
         answer_anchors=["副本数下降", "线程池", "扩容"],
         key_points=["副本减少导致剩余副本压力增大", "可能级联引发线程池耗尽（rb-threadpool-exhaust）", "应急扩容分摊压力"]),
    dict(id=14, intent="rag", difficulty="hard", qtype="multi-hop",
         question="一条新上线的慢 SQL 如果不处理，可能逐步演变成什么故障？请说出完整的故障传导链。",
         golden_doc_ids=["rb-mysql-slow-query", "rb-mysql-conn-pool", "rb-full-link-timeout"],
         answer_anchors=["连接池", "超时", "雪崩"],
         key_points=["慢 SQL 占用连接 → 连接池耗尽（rb-mysql-conn-pool）", "上游等待超时 → 线程被占满", "最终演变为全链路超时雪崩（rb-full-link-timeout）"]),
    dict(id=15, intent="rag", difficulty="hard", qtype="multi-hop",
         question="Redis 主从切换引发连接风暴，这和缓存雪崩在风险上有什么共同点？需要同时防范什么？",
         golden_doc_ids=["rb-redis-failover", "rb-cache-avalanche"],
         answer_anchors=["集群整体不可用", "穿透到 DB", "本地缓存兜底"],
         key_points=["两者都会造成缓存层瞬时不可用", "请求穿透到 DB 可能打垮数据库", "都需要网关限流 + 本地缓存兜底"]),
    dict(id=16, intent="rag", difficulty="hard", qtype="multi-hop",
         question="网关出现 502，但直连上游服务 IP 却一切正常，这时候应该往什么方向查？",
         golden_doc_ids=["rb-gateway-5xx", "rb-dns-failure", "rb-gateway-conn-exhaust"],
         answer_anchors=["网关自身", "DNS", "连接数"],
         key_points=["排除上游问题，转向网关自身", "检查网关连接数是否耗尽（rb-gateway-conn-exhaust）", "检查 DNS 解析（rb-dns-failure）"]),
    dict(id=17, intent="rag", difficulty="medium", qtype="multi-hop",
         question="支付回调积压时，什么操作绝对禁止？正确的处置顺序是什么？",
         golden_doc_ids=["rb-pay-callback-timeout", "rb-mq-backlog"],
         answer_anchors=["禁止", "对账兜底", "扩容消费者"],
         key_points=["禁止重启支付服务（打断资金流水）", "先扩容消费者按 rb-mq-backlog 清积压", "超时订单走对账兜底修正状态"]),
    dict(id=18, intent="rag", difficulty="medium", qtype="multi-hop",
         question="报表任务为什么会重复执行？根因和两个层面的修复分别是什么？",
         golden_doc_ids=["rb-cron-duplicate", "svc-report-job"],
         answer_anchors=["分布式锁", "幂等", "Airflow"],
         key_points=["Airflow 重试 + Celery 无幂等", "入口加 Redis 分布式锁 SET NX PX", "写入改为幂等（ON DUPLICATE KEY UPDATE）"]),
    dict(id=19, intent="rag", difficulty="medium", qtype="multi-hop",
         question="大促前预热脚本如果给所有缓存 key 设置了完全相同的 TTL，会埋下什么隐患？正确的做法是什么？",
         golden_doc_ids=["pol-capacity-preheat", "rb-cache-avalanche"],
         answer_anchors=["集中过期", "雪崩", "抖动"],
         key_points=["相同 TTL 会导致 key 集中过期触发缓存雪崩", "TTL 必须加 ±10% 随机抖动", "预热后命中率需 ≥ 95% 才能开闸"]),

    # ---------------- 口语化改写（6）----------------
    dict(id=20, intent="rag", difficulty="medium", qtype="colloquial",
         question="我们的机器老是自己重启，内存好像也吃满了，咋整啊？",
         golden_doc_ids=["rb-pod-oomkilled"],
         answer_anchors=["OOMKilled", "heap dump", "75%"],
         key_points=["识别为容器 OOMKilled 反复重启", "先区分泄漏还是流量", "泄漏抓 heap dump；瞬时调 limit 并把堆设为 75% 以内"]),
    dict(id=21, intent="rag", difficulty="medium", qtype="colloquial",
         question="数据库老是连不上，程序里报拿不到连接，是不是数据库挂了？",
         golden_doc_ids=["rb-mysql-conn-pool"],
         answer_anchors=["Threads_connected", "慢 SQL", "连接池"],
         key_points=["识别为连接池耗尽而非数据库宕机", "先看 processlist 是 Sleep 多还是 Running 多", "临时调大应用侧连接池止血"]),
    dict(id=22, intent="rag", difficulty="easy", qtype="colloquial",
         question="上传文件一直失败，提示了个 507，这是哪里的问题？",
         golden_doc_ids=["err-file-service", "rb-disk-full"],
         answer_anchors=["FILE-507-001", "MinIO", "磁盘"],
         key_points=["FILE-507-001 = 对象存储空间不足", "MinIO 集群磁盘使用率超过 95%", "按 rb-disk-full 清理或扩容"]),
    dict(id=23, intent="rag", difficulty="easy", qtype="colloquial",
         question="网站打不开了，刷新全是 502，这是哪里坏了？",
         golden_doc_ids=["rb-gateway-5xx", "faq-05"],
         answer_anchors=["上游", "error.log", "Nacos"],
         key_points=["502 = 上游连接失败", "先看 nginx error.log 定位具体上游", "检查上游 Pod 与 Nacos 实例列表"]),
    dict(id=24, intent="rag", difficulty="medium", qtype="colloquial",
         question="有人反馈站内搜索搜出来的东西变少了，是不是搜索坏了？",
         golden_doc_ids=["rb-es-cluster-red", "svc-search-service"],
         answer_anchors=["unassigned_shards", "yellow", "red"],
         key_points=["结果不完整通常是 ES 分片未分配", "查 _cluster/health 状态", "red 需立即处理，yellow 可容忍"],
         notes_extra="口语中不会提 ES，考察 query 改写能否映射到搜索域"),
    dict(id=25, intent="rag", difficulty="medium", qtype="colloquial",
         question="有个用户投诉说一天收到了几十条一模一样的短信，怎么回事？",
         golden_doc_ids=["svc-notify-service", "err-notify-service"],
         answer_anchors=["10 条/分钟", "20 条/小时", "频控"],
         key_points=["短信频控为 10 条/分钟、20 条/小时", "超限静默丢弃", "频控失效或 key 过期窗口重叠会导致重复下发"]),

    # ---------------- 边界与否定（4）----------------
    dict(id=26, intent="rag", difficulty="medium", qtype="edge",
         question="502 和 504 的区别是什么？各自的排查第一步分别是什么？",
         golden_doc_ids=["faq-05", "rb-gateway-5xx"],
         answer_anchors=["connect() failed", "upstream timed out", "error.log"],
         key_points=["502 = 无法连接上游；504 = 连上但读超时", "都在 nginx error.log 里找关键字", "502 查连接、504 查上游耗时"]),
    dict(id=27, intent="rag", difficulty="hard", qtype="edge",
         question="风控引擎为什么用 fail-open 而不是 fail-close？这个选择在什么场景下是错的？",
         golden_doc_ids=["svc-risk-engine", "rb-risk-false-positive", "oncall-postmortem"],
         answer_anchors=["100ms", "放行", "误杀"],
         key_points=["规则执行超 100ms 直接放行", "宁可放过不可误杀（交易体验优先）", "资金强校验场景不应 fail-open——需按业务重新评估"]),
    dict(id=28, intent="rag", difficulty="medium", qtype="edge",
         question="磁盘没满但创建文件报 No space left on device，这是什么问题？怎么和真正的磁盘满区分？",
         golden_doc_ids=["rb-inode-exhaust", "rb-disk-full"],
         answer_anchors=["df -i", "IUse%", "小文件"],
         key_points=["inode 耗尽：df -h 正常但 df -i 100%", "海量小文件是常见根因", "按目录 find 统计文件数定位"]),
    dict(id=29, intent="rag", difficulty="hard", qtype="edge",
         question="缓存穿透和缓存雪崩都会导致 DB 压力大，怎么区分？处置手段有什么本质不同？",
         golden_doc_ids=["rb-cache-penetration", "rb-cache-avalanche"],
         answer_anchors=["命中率", "不存在的 key", "集中过期"],
         key_points=["雪崩：命中率断崖下跌（大量 key 同时失效）", "穿透：命中率正常但查的都是不存在的 key", "雪崩靠错峰 TTL + 限流；穿透靠空值缓存 + 布隆过滤器"]),

    # ---------------- 多模态（4，notes 标注对应截图）----------------
    dict(id=30, intent="rag", difficulty="medium", qtype="multimodal",
         question="日志截图里出现了 java.lang.OutOfMemoryError: Java heap space，同时 Pod 反复重启，应该按哪个文档处理？第一步做什么？",
         golden_doc_ids=["rb-pod-oomkilled", "cmd-arthas"],
         answer_anchors=["OOMKilled", "heap dump", "扩容"],
         key_points=["对应 rb-pod-oomkilled", "先区分泄漏还是瞬时流量", "泄漏抓 heap dump 用 MAT 分析"],
         image_ref="img-java-oom-heap"),
    dict(id=31, intent="rag", difficulty="hard", qtype="multimodal",
         question="监控大盘截图显示缓存命中率 31.7%、网关 5xx 5.42%、全链路 P99 8.24s 同时告警，这说明什么？此刻最优先的动作是什么？",
         golden_doc_ids=["rb-full-link-timeout", "rb-cache-avalanche"],
         answer_anchors=["雪崩", "断链", "限流"],
         key_points=["多告警级联 = 雪崩特征（缓存先崩→DB→网关→全链路）", "先在网关限流止血，切断级联传播", "再按 P99 恶化时间定位根因服务"],
         image_ref="img-grafana-alert"),
    dict(id=32, intent="rag", difficulty="hard", qtype="multimodal",
         question="运维截图显示 df -h 磁盘使用率只有 43%，但应用创建文件时报 No space left on device，这是什么原因？",
         golden_doc_ids=["rb-inode-exhaust"],
         answer_anchors=["inode", "df -i", "100%"],
         key_points=["inode 耗尽而非空间耗尽", "df -i 查看 IUse% 是否 100%", "海量小文件需按目录统计并调整切分粒度"],
         image_ref="img-inode-exhaust"),
    dict(id=33, intent="rag", difficulty="medium", qtype="multimodal",
         question="MySQL 报 Too many connections，processlist 里 Sleep 状态的连接有 812 个，这说明什么问题？和慢 SQL 占用连接怎么区分？",
         golden_doc_ids=["rb-mysql-conn-pool", "rb-mysql-slow-query"],
         answer_anchors=["Sleep", "泄漏", "processlist"],
         key_points=["大量 Sleep = 连接泄漏（应用未释放）", "大量 Running = 慢 SQL 占用", "两者处置路径不同：泄漏查代码，慢 SQL 查索引"],
         image_ref="img-mysql-too-many-connections"),

    # ---------------- 工具调用（3）----------------
    dict(id=34, intent="tool", difficulty="easy", qtype="tool",
         question="帮我查一下工单 INC-2026-0412 的处理进度。",
         golden_doc_ids=[], expected_tools=["query_ticket_by_no"],
         golden_ticket_nos=["INC-2026-0412"],
         answer_anchors=["OOMKilled", "处理中", "@trade-oncall"],
         key_points=["INC-2026-0412 为订单服务 OOMKilled 事件", "当前状态「处理中」", "责任方 @trade-oncall"]),
    dict(id=35, intent="tool", difficulty="easy", qtype="tool",
         question="资产编码 SRV-0088 这台机器现在是什么状态？上面跑着哪些服务？",
         golden_doc_ids=[], expected_tools=["get_asset_by_code"],
         answer_anchors=["app-prod-01", "10.0.1.11", "online"],
         key_points=["SRV-0088 = app-prod-01", "IP 10.0.1.11，状态 online", "承载 order-service 与 user-center"]),
    dict(id=36, intent="tool", difficulty="medium", qtype="tool",
         question="查一下库存服务（inventory-service）今天有没有未关闭的告警。",
         golden_doc_ids=[], expected_tools=["query_alerts_by_service"],
         answer_anchors=["inventory-service"],
         key_points=["按服务维度查询告警", "inventory-service 当前无未关闭告警", "无告警时应明确回答「无」而不是编造"]),

    # ---------------- 混合（RAG + 工具，3）----------------
    dict(id=37, intent="hybrid", difficulty="medium", qtype="hybrid",
         question="MySQL 连接数超过多少会告警？另外把数据库连接相关的历史工单也给我看一下。",
         golden_doc_ids=["alert-p1-db-conn", "rb-mysql-conn-pool"],
         expected_tools=["query_ticket_by_keyword"],
         golden_ticket_nos=["INC-2026-0415"],
         answer_anchors=["85%", "INC-2026-0415"],
         key_points=["超过 max_connections 的 85% 告警（alert-p1-db-conn）", "相关工单 INC-2026-0415（慢查询导致连接池耗尽，已关闭）", "知识 + 工单两路都要命中"]),
    dict(id=38, intent="hybrid", difficulty="medium", qtype="hybrid",
         question="短信的频控规则是什么？用户重复收到短信的投诉工单处理到哪一步了？",
         golden_doc_ids=["svc-notify-service", "err-notify-service"],
         expected_tools=["query_ticket_by_service", "query_ticket_by_keyword"],
         golden_ticket_nos=["INC-2026-0420"],
         answer_anchors=["10 条/分钟", "INC-2026-0420", "处理中"],
         key_points=["频控 10 条/分钟、20 条/小时", "工单 INC-2026-0420 状态「处理中」", "根因是渠道重试与频控 TTL 窗口重叠"]),
    dict(id=39, intent="hybrid", difficulty="hard", qtype="hybrid",
         question="ES 集群什么状态算 red？之前那次 ES red 的事故工单最后是怎么解决的？",
         golden_doc_ids=["rb-es-cluster-red", "alert-p0-es-red"],
         expected_tools=["query_ticket_by_service"],
         golden_ticket_nos=["INC-2026-0433"],
         answer_anchors=["主分片", "INC-2026-0433", "磁盘水位"],
         key_points=["red = 存在未分配的主分片", "工单 INC-2026-0433：日志索引打满磁盘（>85%）触发", "清理后恢复，验证知识答案与工单结论一致"]),

    # ---------------- 闲聊（1）----------------
    dict(id=40, intent="chitchat", difficulty="easy", qtype="chitchat",
         question="你好，你是谁？你能帮我做什么？",
         golden_doc_ids=[],
         answer_anchors=["运维助手"],
         key_points=["简短自我介绍", "不触发检索与工具调用", "说明可查工单/资产/解答运维问题"]),

    # ---------------- 长文档专项（12）----------------
    # 对应 data/source/longform.py 的 5 篇长文档。这批题不是为了"再加几道"，
    # 而是验证三件只有长文档才能暴露的事：
    #   1. 超长表格被按行拆成多块后，跨块的事实是否还能被完整召回（q-44/47/51）；
    #   2. 三层标题嵌套下，子节内容是否会被错误归属到父节（q-43/46/50）；
    #   3. 长文本稀释关键词密度后 BM25 衰减到多少 —— 这是引入向量召回与
    #      重排的量化依据，而不是"觉得应该加"（这批题会压低 BM25 基线）。
    # 注意：golden_doc_ids 只锚定长文档本身，不混入短文档，避免把
    # "长文档召回失败"与"跨文档融合失败"两种错误混在一起看不出原因。
    dict(id=41, intent="rag", difficulty="hard", qtype="longdoc",
         question="入库链路的块长上限是多少字符？这个上限是怎么定出来的，为什么不能拍脑袋决定？",
         golden_doc_ids=["guide-ingest-pipeline-design"],
         answer_anchors=["460", "512", "40"],
         key_points=["块长上限取 460 字符", "依据是 bge-small-zh-v1.5 的 max_seq_length=512 token、中文约 1 token/字",
                     "超限部分会被 tokenizer 静默截断，因此给上下文头预留约 40 字符余量"]),
    dict(id=42, intent="rag", difficulty="medium", qtype="longdoc",
         question="一个二级小节的内容超过块长上限时会被怎么处理？切出来的几块之间保留多少重叠，编号长什么样？",
         golden_doc_ids=["guide-ingest-pipeline-design"],
         answer_anchors=["80 字符", "1/3", "重叠"],
         key_points=["按空行分段打包成多个窗口", "窗口之间保留 80 字符重叠，避免操作步骤被硬切",
                     "编号写作 处置步骤(1/3)，让人看出是分段而非独立小节"]),
    dict(id=43, intent="rag", difficulty="hard", qtype="longdoc",
         question="为什么入库的点 id 要用 uuid5(chunk_id) 派生而不是随机生成？重跑一次入库时旧数据是怎么处理的？",
         golden_doc_ids=["guide-ingest-pipeline-design"],
         answer_anchors=["uuid5", "重复", "kb_version"],
         key_points=["同一份语料重复入库得到完全相同的 id，upsert 覆盖写、不产生重复点",
                     "不做整库删除重建，避免入库期间出现空库窗口",
                     "每次入库把 kb_version 加一，检索按该字段过滤，旧版本数据不被召回"]),
    dict(id=44, intent="rag", difficulty="hard", qtype="longdoc",
         question="2026-05-18 支付网关那次 P0 故障持续了多久？影响多少笔订单？直接资损评估是多少？",
         golden_doc_ids=["pm-payment-outage-2026q2"],
         answer_anchors=["43 分钟", "12.7 万", "8.4 万元"],
         key_points=["14:07 开始、14:50 恢复，持续 43 分钟", "12.7 万笔订单支付失败",
                     "直接资损评估 8.4 万元"]),
    dict(id=45, intent="rag", difficulty="hard", qtype="longdoc",
         question="支付网关那次 P0 的直接原因是什么？为什么说连接池配置和慢查询这两个问题单独存在都不会造成 P0？",
         golden_doc_ids=["pm-payment-outage-2026q2"],
         answer_anchors=["连接池", "20", "慢查询"],
         key_points=["连接池最大连接数由 100 下调为 20，调整时只看 QPS 均值没看并发连接峰值",
                     "一条未走索引的慢查询耗时 2.8s，让每个连接被占用更久",
                     "连接数充足时慢查询只表现为偶发慢，连接数被压缩后直接耗尽连接池，两者叠加才成为 P0"]),
    dict(id=46, intent="rag", difficulty="hard", qtype="longdoc",
         question="那次 P0 里监控为什么没能提前发现？最早出现的信号是什么指标，为什么没被接住？",
         golden_doc_ids=["pm-payment-outage-2026q2"],
         answer_anchors=["连接等待", "340ms", "db_pool_wait_seconds"],
         key_points=["14:01 连接等待时间 P99 已由 12ms 升至 340ms，是最早最直接的信号",
                     "该指标根本没有采集，告警规则只覆盖错误率与响应时间",
                     "告警系统与变更系统相互独立，告警不带最近 15 分钟有变更这个上下文"]),
    dict(id=47, intent="rag", difficulty="hard", qtype="longdoc",
         question="2026 下半年容量规划里 12 个服务有多少个余量为负？余量这一列是怎么算出来的？",
         golden_doc_ids=["spec-capacity-plan-2026h2"],
         answer_anchors=["9 个", "1.3", "0.7"],
         key_points=["12 个服务里有 9 个余量为负", "余量 = 副本数 × 单副本 QPS ÷ (预测峰值 × 1.3) − 1",
                     "单副本容量 = 压测实测值 × 0.7 回退系数，安全系数取 1.3"]),
    dict(id=48, intent="rag", difficulty="hard", qtype="longdoc",
         question="容量规划里的扩容优先级是按什么排序的？为什么运维助手缺口接近最大却只排在 P2？",
         golden_doc_ids=["spec-capacity-plan-2026h2"],
         answer_anchors=["业务影响", "内部工具", "P0"],
         key_points=["按 余量缺口 × 业务影响 排序，而不是单纯按缺口大小",
                     "ops-agent 缺口 −59% 属内部工具、不占交易链路，挂掉不影响任何一笔交易",
                     "P0 是 search/gateway/payment/order 四个大促主链路必经节点，缺口均在 35% 以上"]),
    dict(id=49, intent="rag", difficulty="medium", qtype="longdoc",
         question="数据库九阶段变更流程适用于什么量级的表？低于这个量级的变更走什么通道？",
         golden_doc_ids=["guide-db-migration-playbook"],
         answer_anchors=["1000 万行", "20GB", "轻量通道"],
         key_points=["表数据量 ≥ 1000 万行，或单表体积 ≥ 20GB",
                     "低于该量级走轻量通道，不必套用九阶段",
                     "判断标准是低峰期直接 ALTER 的锁表时间是否可接受"]),
    dict(id=50, intent="rag", difficulty="hard", qtype="longdoc",
         question="数据回填阶段为什么要按主键分片并刻意加 sleep？读切换的灰度档位和每档最短观察时长是多少？",
         golden_doc_ids=["guide-db-migration-playbook"],
         answer_anchors=["5000", "0.2", "15 分钟"],
         key_points=["按 id 区间分片、每批 5000 行，避免一次大事务撑爆 binlog 并导致从库延迟",
                     "sleep 0.2 是刻意加的，给从库追赶时间",
                     "读切换按 1% → 10% → 50% → 100% 灰度，每一档至少观察 15 分钟"]),
    dict(id=51, intent="rag", difficulty="hard", qtype="longdoc",
         question="链路追踪规范 v2 的采样策略和 v1 相比有什么变化？低流量高价值的支付链路采用什么采样率，为什么？",
         golden_doc_ids=["spec-tracing-standard-v2"],
         answer_anchors=["分级", "100%", "1%"],
         key_points=["v1 是全局统一比例（固定 1%），v2 改为按服务分级采样",
                     "支付、退款、结算属全额采样 100%，涉及资损、任何一条链路都可能需要回溯",
                     "统一的 1% 会让低流量高价值链路样本过少、出问题无法回溯，高流量低价值链路反而占满存储"]),
    dict(id=52, intent="rag", difficulty="hard", qtype="longdoc",
         question="线程池和异步任务为什么最容易断链？正确做法是什么，断链后链路图会表现成什么样？",
         golden_doc_ids=["spec-tracing-standard-v2"],
         answer_anchors=["Context.current()", "wrap", "两棵树"],
         key_points=["提交任务时必须显式捕获当前上下文并在任务内恢复，如 executor.submit(ctx.wrap(() -> doWork()))",
                     "直接 executor.submit(() -> doWork()) 会丢失上下文",
                     "症状隐蔽：链路不是断了而是分裂成两棵树，看链路图容易误判为两个独立请求"]),
]

# ---- 舰队扩展（53 起）：与 data/source/fleet.py 同源派生 ----
# 锚点与 golden 全部由舰队模板常量派生，与文档正文共享同一套公式，
# check_annotation 第 8 条（锚点逐字出现在正文）天然可过。
from data.source.fleet import fleet_questions as _fleet_questions  # noqa: E402

Q.extend(_fleet_questions(len(Q) + 1))


def main() -> int:
    # ---- 校验 1：golden_doc_ids 必须存在于语料清单 ----
    if not MANIFEST.exists():
        print("FAIL: 未找到语料清单，请先运行 python -m scripts.gen_corpus")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    known_docs = {d["doc_id"] for d in manifest["documents"]}
    known_images = {i["image_id"] for i in json.loads((ROOT / "data/raw/images/MANIFEST.json").read_text(encoding="utf-8"))["images"]}
    ticket_nos = {t["ticket_no"] for t in MOCK_RECORDS["tickets"]}

    errors: list[str] = []
    for q in Q:
        for did in q.get("golden_doc_ids", []):
            if did not in known_docs:
                errors.append(f"q-{q['id']}: golden_doc_id 不存在 -> {did}")
        for img in [q.get("image_ref")] if q.get("image_ref") else []:
            if img not in known_images:
                errors.append(f"q-{q['id']}: image_ref 不存在 -> {img}")
        for t in q.get("expected_tools", []):
            if t not in KNOWN_TOOLS:
                errors.append(f"q-{q['id']}: 未定义的工具 -> {t}")
        for tn in q.get("golden_ticket_nos", []):
            if tn not in ticket_nos:
                errors.append(f"q-{q['id']}: 工单号不在 mock 数据中 -> {tn}")

    if errors:
        print("FAIL: 标注与语料/工具/mock 数据不一致：")
        for e in errors:
            print(f"  - {e}")
        return 1

    # ---- 落盘 questions.json（去掉内部字段）----
    questions = []
    for q in Q:
        item = {k: v for k, v in q.items() if k not in {"qtype", "image_ref", "notes_extra"}}
        item["notes"] = q.get("notes_extra") or f"检索类型: {q['qtype']}"
        if q.get("image_ref"):
            item["notes"] += f"；对应截图 {q['image_ref']}"
        questions.append(item)
    OUT_QUESTIONS.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 落盘 mock 数据 ----
    OUT_MOCK.write_text(json.dumps(MOCK_RECORDS, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 统计 ----
    print(f"已生成 {len(questions)} 条标注 -> {OUT_QUESTIONS.relative_to(ROOT).as_posix()}")
    print(f"mock 数据（{len(MOCK_RECORDS['tickets'])} 工单 + {len(MOCK_RECORDS['assets'])} 资产） -> {OUT_MOCK.relative_to(ROOT).as_posix()}")
    print("\n意图分布：", dict(Counter(q["intent"] for q in Q)))
    print("难度分布：", dict(Counter(q["difficulty"] for q in Q)))
    print("检索类型：", dict(Counter(q["qtype"] for q in Q)))
    multi_golden = sum(1 for q in Q if len(q.get("golden_doc_ids", [])) > 1)
    print(f"多 golden 文档的题（考验 rerank 区分度）：{multi_golden} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
