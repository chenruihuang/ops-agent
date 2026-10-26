"""服务舰队扩展 —— 第二知识源层（程序化生成，与手写源同一套渲染器与校验器）。

定位与原则
----------
手写源（services/runbooks/policies_arch/operations/longform）覆盖 12 个核心服务，
共 110 篇文档。本模块在其之上扩展一支 **60 个服务** 的"舰队"，覆盖消息 / 数据 /
风控 / 营销 / 物流 / 媒体 / 开放平台 / 平台组件 8 个新域。

为什么用程序化生成而不是继续手写：
  1. 单一事实源不变：舰队服务的所有事实（限流、错误码、阈值、值班组）
     都在下面的表与模板里定义一次，文档 / 评估标注 / 截图三方派生，
     锚点校验（check_annotation 第 8 条）天然可过。
  2. 数字必须"硬"：每个服务从行号派生出独立的确定性数字（限流值、阈值、
     副本数变体），保证 1010 篇文档没有两篇完全同文，否则检索评估
     会退化成"在近重复语料里猜"。
  3. 引用零悬空：交叉引用只指向（a）舰队自己生成的 id，（b）手写源里
     已确认存在的全局文档（见 _GLOBAL_RB / _GLOBAL_REF 白名单）。

装配方式：data/source/__init__.py 在导入手写源**之后**调用 apply()，
把手写源列表原位扩展（SERVICES.append / ERROR_CODES.update / ...）。
longform.py 在 apply() 之前导入，其容量基线表仍基于 12 个核心服务，不受影响。
"""

from __future__ import annotations

# =====================================================================
# 一、服务舰队定义表（60 个服务，紧凑元组：每行一个服务）
# (key, name, domain, stack, port, replicas, tier, code, oncall, mids, upstreams, note)
# tier: core / normal / edge（决定 SLO 与告警级别）
# =====================================================================

_FLEET_TABLE: list[tuple] = [
    # ---------------- 消息域（8） ----------------
    ("msg-push", "消息推送中心", "消息域", "Java 17 / Spring Boot 3.2", 8101, 6, "core", "MSG",
     "@msg-oncall（消息域）", ["Kafka 3.6（push-task-topic，24 分区）", "Redis Cluster（设备令牌，db 5）"],
     ["user-center:8081", "push-channel-gw(渠道抽象层)", "device_db(MySQL 8.0 一主两从)"],
     "推送是外呼通道的总闸门，全站触达都从这里走，降级顺序见 obs-msg-push。"),
    ("msg-inbox", "站内信服务", "消息域", "Java 17 / Spring Boot 3.2", 8102, 4, "normal", "INB",
     "@msg-oncall（消息域）", ["Redis Cluster（未读数计数器）", "Kafka 3.6（inbox-event-topic）"],
     ["inbox_db(MongoDB 6.0 副本集)", "msg-push(内部 gRPC)"],
     "未读数用 Redis 计数器异步落库，对账任务每天 03:00 跑，见 spec-msg-inbox。"),
    ("msg-sms", "短信网关", "消息域", "Go 1.22 / gin", 8103, 4, "core", "SMS",
     "@msg-oncall（消息域）", ["Redis Cluster（频控计数器）", "Kafka 3.6（sms-outbox-topic）"],
     ["carrier-proxy(三大运营商通道)", "msg-template(内部 gRPC)"],
     "三条运营商通道主备切换，通道健康度每 30s 探测一次，见 alert-msg-sms-res。"),
    ("msg-mail", "邮件投递服务", "消息域", "Python 3.12 / FastAPI", 8104, 2, "edge", "MAIL",
     "@msg-oncall（消息域）", ["Redis Cluster（去重集合）"],
     ["smtp-relay(企业邮件中继)", "msg-template(内部 gRPC)"],
     "邮件时效要求低（分钟级），是消息域唯一允许排队 5 分钟的服务。"),
    ("msg-ws-gw", "长连接网关", "消息域", "Go 1.22 / gorilla/websocket", 8105, 8, "core", "WSG",
     "@msg-oncall（消息域）", ["Redis Cluster（连接路由表）"],
     ["msg-push(内部 gRPC)", "conn-state-db(Redis Cluster)"],
     "单副本 10 万长连接，连接路由表写 Redis，扩缩容必须先摘流量，见 guide-msg-ws-gw。"),
    ("msg-queue-mgr", "队列治理平台", "消息域", "Java 17 / Spring Boot 3.2", 8106, 2, "normal", "QMG",
     "@msg-oncall（消息域）", ["Kafka 3.6（治理元数据）"],
     ["kafka-cluster(全站集群)"],
     "只读治理面：Topic 申请、分区均衡、消费组告警聚合，不碰数据面。"),
    ("msg-template", "消息模板中心", "消息域", "Java 17 / Spring Boot 3.2", 8107, 4, "normal", "TPL",
     "@msg-oncall（消息域）", ["Redis Cluster（模板缓存，命中率 99%+）"],
     ["template_db(MySQL 8.0 一主两从)"],
     "模板变更走审批流，缓存 5 分钟内生效，见 pol-config-change。"),
    ("msg-broadcast", "批量广播服务", "消息域", "Go 1.22 / gin", 8108, 3, "edge", "BCT",
     "@msg-oncall（消息域）", ["Kafka 3.6（broadcast-task-topic）"],
     ["msg-push(内部 gRPC)", "crowd_db(ClickHouse 2 分片)"],
     "百万级广播拆分为 5000/批的任务分片，限速 2 万条/分钟，见 spec-msg-broadcast。"),
    # ---------------- 数据域（8） ----------------
    ("ledger-core", "记账核心服务", "数据域", "Java 21 / Spring Boot 3.3", 8201, 6, "core", "LDG",
     "@data-oncall（数据域）", ["Kafka 3.6（ledger-entry-topic）", "Redis Cluster（幂等键）"],
     ["ledger_db(MySQL 8.0 8 库 64 表分片)", "order-service:8082"],
     "记账幂等键 = 业务单号 + 分录类型，重复投递直接丢弃并计数，见 spec-ledger-core。"),
    ("ledger-settle", "结算服务", "数据域", "Java 21 / Spring Boot 3.3", 8202, 4, "core", "STL",
     "@data-oncall（数据域）", ["Kafka 3.6（settle-bill-topic）"],
     ["settle_db(MySQL 8.0 一主两从)", "ledger-core(内部 gRPC)"],
     "日切窗口 00:10-00:40，日切期间禁止发布，见 pol-config-change 与值班约定。"),
    ("data-sync", "数据同步服务", "数据域", "Go 1.22 / gin", 8203, 3, "normal", "DSY",
     "@data-oncall（数据域）", ["Kafka 3.6（cdc-event-topic，36 分区）"],
     ["binlog-proxy(Canal 集群)", "dw-staging(ClickHouse)"],
     "同步延迟阈值 30s，超过即告警；回补任务必须走限速白名单，见 rb-mq-backlog。"),
    ("data-etl", "离线计算调度", "数据域", "Python 3.12 / Airflow 2.9", 8204, 2, "normal", "ETL",
     "@data-oncall（数据域）", ["Airflow 2.9（元数据 MySQL）"],
     ["dw-ods(HDFS/ClickHouse)", "data-sync(内部 gRPC)"],
     "凌晨批量任务槽位 64 个，任务失败重试 2 次后人工介入，见 cmd-infra-job 同类处置。"),
    ("data-report", "报表服务", "数据域", "Java 17 / Spring Boot 3.2", 8205, 3, "edge", "RPT",
     "@data-oncall（数据域）", ["Redis Cluster（报表结果缓存 10 分钟）"],
     ["report_db(ClickHouse 2 分片)"],
     "报表查询强制带时间范围，全表扫描类查询在网关层直接拒绝。"),
    ("data-profile", "用户画像服务", "数据域", "Java 17 / Spring Boot 3.2", 8206, 4, "normal", "PRF",
     "@data-oncall（数据域）", ["Redis Cluster（标签缓存）", "Kafka 3.6（profile-update-topic）"],
     ["profile_db(ClickHouse)", "data-sync(内部 gRPC)"],
     "标签更新最终一致，实时性要求高的场景必须走 data-sync 的 CDC 通道。"),
    ("data-dict", "配置字典服务", "数据域", "Go 1.22 / gin", 8207, 3, "normal", "DCT",
     "@data-oncall（数据域）", ["Redis Cluster（字典缓存）"],
     ["dict_db(MySQL 8.0 单主)"],
     "字典变更全量审计，误变更回滚窗口 10 分钟，见 pm-data-dict 复盘。"),
    ("data-archive", "数据归档服务", "数据域", "Java 17 / Spring Boot 3.2", 8208, 2, "edge", "ARC",
     "@data-oncall（数据域）", ["Kafka 3.6（archive-task-topic）"],
     ["archive_oss(对象存储)", "order_db(冷库实例)"],
     "归档任务只能削峰在业务低峰执行，白天限速 500 行/秒，见 spec-data-archive。"),
    # ---------------- 风控域（6） ----------------
    ("risk-decision", "实时决策引擎", "风控域", "Java 21 / Spring Boot 3.3", 8301, 8, "core", "RSK",
     "@risk-oncall（风控域）", ["Redis Cluster（特征缓存，P99 < 5ms）", "Kafka 3.6（risk-event-topic）"],
     ["feature-store(Redis 集群)", "risk-rules(内部 gRPC)", "risk-model(内部 gRPC)"],
     "决策链路 P99 预算 50ms，特征查询超 10ms 即降级走默认规则，见 obs-risk-decision。"),
    ("risk-rules", "规则管理服务", "风控域", "Java 17 / Spring Boot 3.2", 8302, 3, "normal", "RUL",
     "@risk-oncall（风控域）", ["Redis Cluster（规则版本缓存）"],
     ["rules_db(MySQL 8.0 一主两从)", "risk-decision(内部 gRPC)"],
     "规则上线必须先影子跑 24h，误杀率超 0.5% 禁止全量，见 pol-gray-release。"),
    ("risk-list", "名单服务", "风控域", "Go 1.22 / gin", 8303, 4, "core", "BLK",
     "@risk-oncall（风控域）", ["Redis Cluster（名单布隆过滤器）"],
     ["list_db(MySQL 8.0 一主两从)"],
     "黑白名单缓存 60 秒，加名单接口限流 10 QPS 防误操作放大。"),
    ("risk-device", "设备指纹服务", "风控域", "Java 17 / Spring Boot 3.2", 8304, 4, "normal", "DEV",
     "@risk-oncall（风控域）", ["Kafka 3.6（device-event-topic）"],
     ["device_db(MongoDB 6.0 副本集)"],
     "指纹碰撞率监控阈值 0.1%，突增说明 SDK 版本异常，见 alert-risk-device-res。"),
    ("risk-model", "模型推理服务", "风控域", "Python 3.12 / FastAPI + ONNX", 8305, 6, "core", "MDL",
     "@risk-oncall（风控域）", ["Redis Cluster（特征快照）"],
     ["model-registry(模型仓库)", "feature-store(Redis 集群)"],
     "推理超时 80ms 即返回降级分，模型回滚保留最近 5 个版本热备，见 guide-risk-model。"),
    ("risk-audit", "风控审计服务", "风控域", "Java 17 / Spring Boot 3.2", 8306, 2, "normal", "AUD",
     "@risk-oncall（风控域）", ["Kafka 3.6（audit-log-topic）"],
     ["audit_db(ClickHouse)"],
     "审计链路只增不改，留存 3 年；查询接口限流 5 QPS。"),
    # ---------------- 营销域（8） ----------------
    ("mkt-coupon", "优惠券服务", "营销域", "Java 21 / Spring Boot 3.3", 8401, 8, "core", "CPN",
     "@mkt-oncall（营销域）", ["Redis Cluster（券库存 Lua 原子扣减）", "Kafka 3.6（coupon-event-topic）"],
     ["coupon_db(MySQL 8.0 8 库 64 表分片)", "ledger-core(内部 gRPC)"],
     "大促前必须做券库存预热，见 pol-capacity-preheat；超发按 P0 事故处理。"),
    ("mkt-activity", "活动管理服务", "营销域", "Java 17 / Spring Boot 3.2", 8402, 4, "normal", "ACT",
     "@mkt-oncall（营销域）", ["Redis Cluster（活动配置缓存）"],
     ["activity_db(MySQL 8.0 一主两从)", "mkt-coupon(内部 gRPC)"],
     "活动开始前 30 分钟配置冻结，紧急下线走 pol-emergency。"),
    ("mkt-red-envelope", "红包服务", "营销域", "Java 21 / Spring Boot 3.3", 8403, 6, "core", "RED",
     "@mkt-oncall（营销域）", ["Redis Cluster（红包池）", "Kafka 3.6（red-envelope-topic）"],
     ["red_db(MySQL 8.0 分库分表)", "ledger-core(内部 gRPC)"],
     "红包拆分算法在服务端完成，客户端只传拆分请求，防重放见 err-mkt-red-envelope。"),
    ("mkt-referral", "分享裂变服务", "营销域", "Java 17 / Spring Boot 3.2", 8404, 4, "normal", "REF",
     "@mkt-oncall（营销域）", ["Redis Cluster（邀请关系缓存）"],
     ["referral_db(MySQL 8.0 一主两从)", "mkt-coupon(内部 gRPC)"],
     "裂变奖励发放 T+1 对账，刷单识别规则同步到 risk-decision。"),
    ("mkt-point", "积分服务", "营销域", "Java 17 / Spring Boot 3.2", 8405, 4, "normal", "PNT",
     "@mkt-oncall（营销域）", ["Kafka 3.6（point-event-topic）", "Redis Cluster（余额缓存）"],
     ["point_db(MySQL 8.0 一主两从)", "ledger-core(内部 gRPC)"],
     "积分即资产：变动流水与余额双写，日终对账不平即冻结发放，见 pm-mkt-point。"),
    ("mkt-prize", "抽奖服务", "营销域", "Go 1.22 / gin", 8406, 4, "normal", "PRZ",
     "@mkt-oncall（营销域）", ["Redis Cluster（概率表与库存）"],
     ["prize_db(MySQL 8.0 单主)", "mkt-coupon(内部 gRPC)"],
     "概率配置热更新延迟 10 秒内生效，库存为零自动降级为谢谢参与。"),
    ("mkt-abtest", "实验平台", "营销域", "Java 17 / Spring Boot 3.2", 8407, 3, "normal", "ABT",
     "@mkt-oncall（营销域）", ["Redis Cluster（分流配置）"],
     ["abtest_db(MySQL 8.0 单主)"],
     "同一流量域最多 8 层正交实验，分流哈希盐变更会污染全部实验，见 spec-mkt-abtest。"),
    ("mkt-journey", "触达旅程引擎", "营销域", "Java 17 / Spring Boot 3.2", 8408, 4, "edge", "JRN",
     "@mkt-oncall（营销域）", ["Kafka 3.6（journey-step-topic）"],
     ["journey_db(MySQL 8.0 一主两从)", "msg-push(内部 gRPC)", "data-profile(内部 gRPC)"],
     "旅程节点执行延迟容忍 5 分钟，重入幂等键 = 用户 + 旅程 + 节点。"),
    # ---------------- 物流域（6） ----------------
    ("logi-waybill", "运单服务", "物流域", "Java 21 / Spring Boot 3.3", 8501, 6, "core", "WYB",
     "@logi-oncall（物流域）", ["Kafka 3.6（waybill-event-topic）"],
     ["waybill_db(MySQL 8.0 4 库 32 表分片)", "logi-carrier(内部 gRPC)"],
     "运单号段预分配 10 万/次，号段耗尽告警阈值 20%，见 alert-logi-waybill-res。"),
    ("logi-track", "轨迹服务", "物流域", "Go 1.22 / gin", 8502, 4, "normal", "TRK",
     "@logi-oncall（物流域）", ["Kafka 3.6（track-point-topic，48 分区）"],
     ["track_db(ClickHouse 2 分片)"],
     "轨迹点写入吞吐 5 万点/秒，查询只允许按运单号走索引，禁扫分区。"),
    ("logi-dispatch", "调度服务", "物流域", "Java 17 / Spring Boot 3.2", 8503, 4, "core", "DSP",
     "@logi-oncall（物流域）", ["Redis Cluster（运力池）", "Kafka 3.6（dispatch-task-topic）"],
     ["dispatch_db(MySQL 8.0 一主两从)", "logi-waybill(内部 gRPC)"],
     "调度决策 P99 预算 200ms，超时降级为人工派单池，见 obs-logi-dispatch。"),
    ("logi-warehouse", "仓储服务", "物流域", "Java 17 / Spring Boot 3.2", 8504, 4, "normal", "WHS",
     "@logi-oncall（物流域）", ["Redis Cluster（库存占用）"],
     ["warehouse_db(MySQL 8.0 分库分表)", "logi-waybill(内部 gRPC)"],
     "库存占用与释放必须成对，日终盘点差异 > 0.1% 触发告警。"),
    ("logi-carrier", "承运商对接服务", "物流域", "Java 17 / Spring Boot 3.2", 8505, 3, "normal", "CRR",
     "@logi-oncall（物流域）", ["Kafka 3.6（carrier-callback-topic）"],
     ["carrier-gw(承运商网关)"],
     "承运商接口 SLA 参差不齐，全部调用 3s 超时 + 失败入重试队列，见 rb-full-link-timeout。"),
    ("logi-exception", "异常件服务", "物流域", "Python 3.12 / FastAPI", 8506, 2, "edge", "EXC",
     "@logi-oncall（物流域）", ["Redis Cluster（去重）"],
     ["exception_db(MySQL 8.0 单主)", "logi-track(内部 gRPC)"],
     "异常件自动判定规则命中即建单，人工复核队列每天 09:00 清一次。"),
    # ---------------- 媒体域（6） ----------------
    ("media-upload", "上传接入服务", "媒体域", "Go 1.22 / gin", 8601, 6, "core", "UPL",
     "@media-oncall（媒体域）", ["Redis Cluster（分片会话）"],
     ["media-oss(对象存储)", "media-transcode(内部 gRPC)"],
     "分片大小 8MB，秒传哈希用 SHA-256，上传带宽峰值 2Gbps，见 spec-media-upload。"),
    ("media-transcode", "转码集群", "媒体域", "Go 1.22 / K8s Job 池", 8602, 10, "core", "TRC",
     "@media-oncall（媒体域）", ["Kafka 3.6（transcode-task-topic）"],
     ["media-oss(对象存储)", "gpu-pool(GPU 节点池)"],
     "GPU 池利用率 80% 告警，转码队列积压超 1000 触发扩容，见 rb-mq-backlog。"),
    ("media-cdn-sched", "CDN 调度服务", "媒体域", "Go 1.22 / gin", 8603, 4, "core", "CDS",
     "@media-oncall（媒体域）", ["Redis Cluster（调度策略）"],
     ["cdn-edgewise(边缘节点池)", "media-upload(内部 gRPC)"],
     "调度切换生效时间 60s，回源比例突增说明边缘节点异常，见 alert-media-cdn-sched-res。"),
    ("media-thumb", "截帧封面服务", "媒体域", "Python 3.12 / FastAPI", 8604, 3, "edge", "THM",
     "@media-oncall（媒体域）", ["Redis Cluster（封面缓存）"],
     ["media-oss(对象存储)"],
     "截帧取关键帧 + 10% 处，失败重试 2 次后降级为默认封面。"),
    ("media-wm", "水印服务", "媒体域", "Go 1.22 / gin", 8605, 3, "edge", "WMK",
     "@media-oncall（媒体域）", ["Redis Cluster（水印模板缓存）"],
     ["media-oss(对象存储)", "media-transcode(内部 gRPC)"],
     "水印在转码管线内完成，独立加水印只用于存量视频修复。"),
    ("media-player", "播放鉴权服务", "媒体域", "Java 17 / Spring Boot 3.2", 8606, 6, "core", "PLY",
     "@media-oncall（媒体域）", ["Redis Cluster（播放令牌）"],
     ["user-center:8081", "media-cdn-sched(内部 gRPC)"],
     "播放令牌 2 小时有效，防盗链校验失败率阈值 5%，见 err-media-player。"),
    # ---------------- 开放平台（6） ----------------
    ("open-api", "开放网关", "开放平台", "Java 21 / Spring Boot 3.3", 8701, 8, "core", "OAPI",
     "@open-oncall（开放平台）", ["Redis Cluster（应用凭据缓存）"],
     ["gateway(内部复用)", "open-auth(内部 gRPC)", "open-quota(内部 gRPC)"],
     "开放流量与内部流量物理隔离，签名校验失败连续 5 次冻结凭据，见 err-open-api。"),
    ("open-auth", "授权服务", "开放平台", "Java 17 / Spring Boot 3.2", 8702, 4, "core", "OAUTH",
     "@open-oncall（开放平台）", ["Redis Cluster（授权码与令牌）"],
     ["auth_db(MySQL 8.0 一主两从)", "user-center:8081"],
     "授权码一次性且 5 分钟有效，刷新令牌滚动失效，见 spec-open-auth。"),
    ("open-app", "应用管理服务", "开放平台", "Java 17 / Spring Boot 3.2", 8703, 3, "normal", "OAPP",
     "@open-oncall（开放平台）", ["Redis Cluster（应用信息缓存）"],
     ["app_db(MySQL 8.0 单主)", "open-auth(内部 gRPC)"],
     "应用上下线走审批，下线后凭据保留 7 天供灰度回切。"),
    ("open-webhook", "回调推送服务", "开放平台", "Go 1.22 / gin", 8704, 4, "normal", "OWBH",
     "@open-oncall（开放平台）", ["Kafka 3.6（webhook-task-topic）"],
     ["webhook_db(MySQL 8.0 一主两从)"],
     "回调失败指数退避重试 6 次（1m/5m/15m/1h/6h/24h），最终失败进死信，见 rb-mq-backlog。"),
    ("open-quota", "配额服务", "开放平台", "Go 1.22 / gin", 8705, 4, "core", "OQTA",
     "@open-oncall（开放平台）", ["Redis Cluster（配额计数器）"],
     ["quota_db(MySQL 8.0 单主)", "open-api(内部 gRPC)"],
     "配额扣减原子化，日配额与秒配额双层限流，超配额返回 OQTA-429-001。"),
    ("open-sandbox", "沙箱环境服务", "开放平台", "Java 17 / Spring Boot 3.2", 8706, 2, "edge", "OSBX",
     "@open-oncall（开放平台）", ["Redis Cluster（沙箱会话）"],
     ["sandbox_db(独立 MySQL 实例)", "open-api(内部 gRPC)"],
     "沙箱数据每日 02:00 重置，禁止生产数据回流沙箱，见 pol-security-incident。"),
    # ---------------- 平台组件（12） ----------------
    ("infra-file", "文件服务", "平台组件", "Go 1.22 / gin", 8801, 4, "normal", "FIL",
     "@infra-oncall（平台组件）", ["Redis Cluster（直连凭证缓存）"],
     ["media-oss(对象存储)"],
     "上传直连凭证 15 分钟有效，大文件走分片通道由 media-upload 承接。"),
    ("infra-seq", "发号器服务", "平台组件", "Java 21 / Spring Boot 3.3", 8802, 6, "core", "SEQ",
     "@infra-oncall（平台组件）", ["Redis Cluster（号段池）"],
     ["seq_db(MySQL 8.0 单主)"],
     "号段双缓冲，本地缓存 1 万号，DB 不可用时存量号段可续 30 分钟，见 rb-mysql-conn-pool。"),
    ("infra-lock", "分布式锁服务", "平台组件", "Java 17 / Spring Boot 3.2", 8803, 4, "core", "LCK",
     "@infra-oncall（平台组件）", ["Redis Cluster（锁状态）"],
     ["redis-cluster(Redis 集群)"],
     "锁默认 30s 自动释放，续期看门狗线程独立于业务线程，见 rb-redis-failover。"),
    ("infra-idgen", "ID 生成服务", "平台组件", "Go 1.22 / gin", 8804, 4, "core", "IDG",
     "@infra-oncall（平台组件）", ["Redis Cluster（worker 注册）"],
     ["zookeeper(协调)"],
     "雪花算法时钟回拨 5ms 内等待、超限报错，NTP 突变是历史事故，见 pm-infra-idgen。"),
    ("infra-job", "任务调度中心", "平台组件", "Java 17 / Spring Boot 3.2", 8805, 3, "normal", "JOB",
     "@infra-oncall（平台组件）", ["Redis Cluster（分片队列）"],
     ["job_db(MySQL 8.0 一主两从)"],
     "任务分片幂等：同一分片同一轮次只执行一次，重复触发见 rb-cron-duplicate。"),
    ("infra-config", "配置中心代理", "平台组件", "Go 1.22 / gin", 8806, 4, "core", "CFG",
     "@infra-oncall（平台组件）", ["Redis Cluster（配置快照）"],
     ["nacos-cluster(Nacos 2.2)"],
     "配置推送延迟 P99 < 3s，快照兜底保证 Nacos 不可用时冷启动可读，见 rb-dns-failure 同类。"),
    ("infra-trace", "链路采集网关", "平台组件", "Go 1.22 / gin", 8807, 6, "normal", "TRA",
     "@infra-oncall（平台组件）", ["Kafka 3.6（span-topic，24 分区）"],
     ["trace-store(ClickHouse)"],
     "采集丢弃率阈值 1%，超过说明下游写入瓶颈，见 obs-trace-spec 与 obs-sampling。"),
    ("infra-metric", "指标网关", "平台组件", "Go 1.22 / gin", 8808, 4, "normal", "MTR",
     "@infra-oncall（平台组件）", ["Redis Cluster（去重窗口）"],
     ["prometheus(远端写入)"],
     "远端写入失败本地缓冲 10 分钟，缓冲满丢最旧保最新，见 obs-metric-naming。"),
    ("infra-billing", "内部计费服务", "平台组件", "Java 17 / Spring Boot 3.2", 8809, 3, "normal", "BIL",
     "@infra-oncall（平台组件）", ["Kafka 3.6（billing-event-topic）"],
     ["billing_db(MySQL 8.0 一主两从)", "ledger-core(内部 gRPC)"],
     "计费流水月结封账后不可改，冲正走负数流水，见 spec-infra-billing。"),
    ("infra-gw-plugin", "网关插件平台", "平台组件", "Java 17 / Spring Boot 3.2", 8810, 4, "normal", "GWP",
     "@infra-oncall（平台组件）", ["Redis Cluster（插件配置）"],
     ["gateway(内部复用)", "nacos-cluster(Nacos 2.2)"],
     "插件热加载失败自动回退上一版本，禁止在网关插件里写同步 IO，见 rb-gateway-5xx。"),
    ("infra-ssr", "服务注册代理", "平台组件", "Go 1.22 / gin", 8811, 4, "core", "SSR",
     "@infra-oncall（平台组件）", ["Redis Cluster（注册表快照）"],
     ["nacos-cluster(Nacos 2.2)"],
     "注册表快照每 10s 刷新，Nacos 集群整体不可用时快照保底 30 分钟，见 alert-infra-ssr-res。"),
    ("infra-ntf-hub", "告警通知汇聚", "平台组件", "Python 3.12 / FastAPI", 8812, 2, "normal", "NAL",
     "@infra-oncall（平台组件）", ["Redis Cluster（聚合窗口）"],
     ["msg-push(内部 gRPC)"],
     "同指纹告警 5 分钟聚合一条，聚合规则变更走 pol-config-change。"),
]

assert len(_FLEET_TABLE) == 60, f"舰队服务数应为 60，实际 {len(_FLEET_TABLE)}"

# =====================================================================
# 二、手写源里已存在、可被舰队文档引用的全局文档白名单
# （check_corpus 会做悬空校验，这里提前把边界钉死）
# =====================================================================

_GLOBAL_RB = {
    "oom": "rb-pod-oomkilled",
    "upstream-timeout": "rb-full-link-timeout",
    "conn-pool": "rb-mysql-conn-pool",
    "mq-backlog": "rb-mq-backlog",
    "cache-penetration": "rb-cache-penetration",
    "threadpool": "rb-threadpool-exhaust",
    "slow-query": "rb-mysql-slow-query",
    "disk-full": "rb-disk-full",
}

# 评估锚点与正文共用的模板常量（保证「锚点逐字出现在正文」机器可验证）
_LIMIT_TEXT = "单用户操作 QPS 限 {qlimit}，超限返回 {code}-429-001"
_SLO_BY_TIER = {
    "core": ("99.95%", "P95 < 300ms", "5xx 错误预算 0.05%/月"),
    "normal": ("99.9%", "P95 < 500ms", "5xx 错误预算 0.1%/月"),
    "edge": ("99.5%", "P95 < 1s", "5xx 错误预算 0.5%/月"),
}

# =====================================================================
# 三、服务档案 / 错误码 派生
# =====================================================================

def _build_fleet_services() -> list[dict]:
    out: list[dict] = []
    for i, row in enumerate(_FLEET_TABLE):
        key, name, domain, stack, port, replicas, tier, code, oncall, mids, upstreams, note = row
        slo_availability, slo_latency, slo_budget = _SLO_BY_TIER[tier]
        qlimit = 20 + (i % 6) * 10          # 单用户限流：20 / 30 / 40 / 50 / 60 / 70
        api_limit = (2 + (i % 4)) * 1000    # 单接口限流：2000~5000 QPS
        mem = 2 + (i % 4)                   # 容器内存 limit：2~5Gi
        out.append(dict(
            key=key, name=name, domain=domain, stack=stack,
            port=f"{port} / {port + 1000} (metrics)",
            replicas=replicas,
            deploy=f"K8s Deployment {key}，命名空间 {domain.split('域')[0] if '域' in domain else 'infra'}",
            repo=f"git.corp.example.com/{domain.split('域')[0] if '域' in domain else 'infra'}/{key}",
            oncall=oncall,
            health="/actuator/health（Java）或 /healthz（Go / Python）",
            runtime=("-Xms1g -Xmx" + str(mem - 1) + "g -XX:+UseG1GC；容器内存 limit " + str(mem) + "Gi")
            if "Java" in stack else ("GOMAXPROCS=4；容器内存 limit " + str(mem) + "Gi")
            if "Go" in stack else ("UVICORN_WORKERS=4；容器内存 limit " + str(mem) + "Gi"),
            slo_availability=slo_availability,
            slo_latency=slo_latency,
            slo_budget=slo_budget,
            upstreams=list(upstreams),
            mids=list(mids),
            limits=[
                _LIMIT_TEXT.format(qlimit=qlimit, code=code),
                f"单接口限流 {api_limit} QPS，超限返回 {code}-429-002",
                f"连接池 maximum-pool-size {20 + (i % 3) * 10}，超时 30s 抛 {code}-503-001",
            ],
            failmodes=[],
            note=note,
        ))
        svc = out[-1]
        # 每个服务 3 个故障模式：从 8 模板池轮转取 3 个（i / i+3 / i+5 保证同服务不重复）
        picks = sorted({_FLEET_FAILMODES[i % 8][0], _FLEET_FAILMODES[(i + 3) % 8][0],
                        _FLEET_FAILMODES[(i + 5) % 8][0]})
        svc["failmodes"] = [(fm_symptom_short(pk, name), f"rb-{key}-{pk}") for pk in picks]
    return out


def fm_symptom_short(fm_key: str, name: str) -> str:
    return {
        "oom": f"容器 OOMKill 反复重启",
        "upstream-timeout": "上游超时引发 5xx 突增",
        "conn-pool": "DB 连接池耗尽",
        "mq-backlog": "消费积压持续增长",
        "cache-penetration": "缓存穿透压垮下游",
        "threadpool": "工作线程池打满",
        "slow-query": "慢查询拖垮数据库",
        "disk-full": "磁盘水位告急",
    }[fm_key]


_ERROR_ROWS = {
    "400": ("参数校验失败", "请求字段缺失或格式非法", "按接口文档修正参数后重试，不可重试原请求", "否"),
    "401": ("凭据无效或过期", "令牌缺失 / 过期 / 签名不匹配", "刷新凭据后重试；连续失败 5 次联系值班排查是否被封禁", "否"),
    "429": ("触发限流", "超过单用户或单接口限流阈值", "按 Retry-After 退避重试，禁止立即重试", "退避后可"),
    "500": ("服务内部错误", "未捕获异常或下游返回非法响应", "携带 trace_id 联系值班；同请求勿盲目重试", "幂等请求可"),
    "503": ("依赖不可用", "连接池耗尽 / 下游熔断 / 容器重启中", "先看服务健康与依赖状态，处置入口见对应 runbook", "退避后可"),
}


def _build_fleet_error_codes(svc: dict, i: int) -> list[tuple]:
    code = svc["key"] and _FLEET_TABLE[i][7]
    rows = []
    for http, (meaning, trigger, advice, retry) in _ERROR_ROWS.items():
        seq = "001" if http in ("400", "429", "500") else "002"
        adv = advice
        if http == "429":
            adv += f"；本服务单接口限流见 svc-{svc['key']}"
        elif http == "500":
            adv += f"，常用命令见 cmd-{svc['key']}"
        elif http == "503":
            adv += f" rb-{svc['key']}-{_FLEET_FAILMODES[i % 8][0]}"
        rows.append((f"{_FLEET_TABLE[i][7]}-{http}-{seq}", meaning, trigger, adv, retry))
    return rows


# 模板池：8 类故障（与 _GLOBAL_RB 一一对应）
_FLEET_FAILMODES = [
    ("oom", "OOMKill 反复重启", "P1"),
    ("upstream-timeout", "上游超时引发 5xx", "P1"),
    ("conn-pool", "DB 连接池耗尽", "P1"),
    ("mq-backlog", "消费积压持续增长", "P2"),
    ("cache-penetration", "缓存穿透压垮下游", "P1"),
    ("threadpool", "工作线程池打满", "P1"),
    ("slow-query", "慢查询拖垮数据库", "P1"),
    ("disk-full", "磁盘水位告急", "P2"),
]


def _failmode_runbook(svc: dict, i: int, fm_key: str) -> dict:
    """按模板生成一篇 runbook。数字全部由服务行号派生，保证逐服务不重复。"""
    key, name = svc["key"], svc["name"]
    code = _FLEET_TABLE[i][7]
    v = i  # 变体因子
    rb_id = f"rb-{key}-{fm_key}"
    canon = _GLOBAL_RB[fm_key]
    is_java = "Java" in svc["stack"]
    common_related = [f"svc-{key}", f"err-{key}", f"alert-{key}-lat", f"cmd-{key}", canon]

    if fm_key == "oom":
        mem = 2 + (v % 4)
        return dict(
            key=rb_id, title=f"{name} 容器 OOMKill 反复重启处置",
            severity="P1", owner=svc["oncall"],
            symptom=(
                f"{name} 容器内存逼近 limit（{mem}Gi 中的 {mem - 1}Gi 以上），"
                + ("进程抛 java.lang.OutOfMemoryError 后被 OOMKill" if is_java else "进程 RSS 超限后被 OOMKill")
                + f"，Pod RestartCount 持续增长，期间伴随 {code}-503-001（依赖不可用）。"),
            impact=f"{name} 间歇性不可用，重启窗口内的请求失败；下游调用方出现超时级联。",
            triage=[
                ("`kubectl top pod -l app=" + key + " --containers`", "内存贴 limit（如 " + str(mem) + "Gi）即确认"),
                ("`kubectl describe pod <pod> | grep -A3 Last State`", "Reason=OOMKilled 且 Exit Code=137 即确认"),
                ("应用侧内存曲线（Grafana）", "看是长期缓涨（泄漏）还是流量尖峰（瞬时）"),
            ],
            causes=[
                "内存泄漏：缓存无界增长 / 大对象未释放（最常见）",
                "容量配置不当：堆或进程内存超过容器 limit 的 75%",
                "瞬时流量放大：批量任务或重试风暴叠加",
                "依赖返回超大响应体未做流式处理",
            ],
            steps=[
                f"① 先止血：将 {name} 副本数由 {svc['replicas']} 临时扩到 {svc['replicas'] * 2}，摊薄单实例内存压力。",
                ("② 抓现场：Java 用 `jmap -dump:live,format=b,file=heap.hprof 1`；非 Java 用 `go tool pprof` 或 py-spy。"),
                "③ 分析 dump 定位持有大对象的数据结构，确认是泄漏还是瞬时。",
                "④ 若为配置不当：把堆/进程内存压到容器 limit 的 75% 以内并滚动重启。",
                "⑤ 泄漏类问题升级研发定位，修复前保持副本扩容 + 告警值守。",
            ],
            rollback="副本扩容可直接缩回；内存参数变更滚动重启即回滚，无数据影响。",
            related=common_related + ["alert-p1-pod-restart"],
            escalate="20 分钟未恢复 -> 升级平台组件负责人；泄漏根因明确前禁止盲目重启掩盖。",
            mttr_target="20 分钟",
        )

    if fm_key == "upstream-timeout":
        t = 1 + (v % 3)
        return dict(
            key=rb_id, title=f"{name} 上游超时引发 5xx 突增处置",
            severity="P1", owner=svc["oncall"],
            symptom=(
                f"{name} 5xx 比例从 0.1% 突增至 {2 + v % 4}%，日志出现大量 connect timeout / read timeout"
                f"（读超时 {t}s），错误码 {code}-503-001 占比最高；上游 P99 同步上涨。"),
            impact=f"{name} 可用性跌破 SLO；调用方收到 5xx，链路尾部服务出现重试放大。",
            triage=[
                ("Grafana 看 5xx 按错误码分布", f"{code}-503-001 占比高 → 依赖问题；{code}-500-001 高 → 本服务问题"),
                ("`kubectl logs <pod> --tail=200 | grep -c Timeout`", "统计超时日志条数，确认影响面"),
                ("上游服务 P99 与错误率", "定位是单一上游恶化还是全链路拥塞"),
            ],
            causes=[
                "单一上游依赖变慢（DB / 缓存 / 下游服务）",
                "全链路流量突增导致排队超时",
                f"读超时配置过短（当前 {t}s，压测基线见 spec-{key}）",
                "重试风暴：下游抖动 + 上层无退避重试叠加",
            ],
            steps=[
                f"① 先止血：在网关层对 {name} 的 5xx 接口开启限流，保护上游恢复。",
                "② 定位超时集中在上游哪一跳：按 trace 链路按 P99 排序。",
                "③ 单一上游问题 → 按该上游对应 runbook 处置；全链路问题 → 按全局链路超时预案处理。",
                "④ 恢复后核对重试放大是否停止（观察上游 QPS 回落）。",
                "⑤ 复盘超时配置与重试策略，纳入容量评审。",
            ],
            rollback="限流规则回滚即恢复；无配置持久化变更。",
            related=common_related,
            escalate="15 分钟未恢复 -> 升级链路上最高 P99 一跳的负责人；入口级故障直接 P1 事故响应。",
            mttr_target="15 分钟",
        )

    if fm_key == "conn-pool":
        pool = 20 + (v % 3) * 10
        return dict(
            key=rb_id, title=f"{name} 连接池耗尽处置",
            severity="P1", owner=svc["oncall"],
            symptom=(
                f"{name} 日志出现 connection is not available / pool exhausted，"
                f"P99 由 {200 + v * 20}ms 涨到 2s 以上，错误码 {code}-503-001 集中出现。"),
            impact=f"{name} 写请求大面积失败；与其共享同一 DB 实例的邻居服务一起变慢。",
            triage=[
                ("连接池指标 active/idle", f"active 长期等于 maximum-pool-size（当前 {pool}）即确认"),
                ("`show processlist` 看会话状态", "大量 Sleep 长连接 → 泄漏；大量 Running → 慢查询"),
                ("DB 侧 Threads_connected", "逼近 max_connections 说明多服务叠加"),
            ],
            causes=[
                "慢 SQL 长期占用连接（按 rb-mysql-slow-query 处理）",
                "连接泄漏：事务未提交 / 连接未归还",
                f"池大小 {pool} 与副本数 {svc['replicas']} 不匹配（扩容后未联动）",
                "DB 主库资源打满整体变慢",
            ],
            steps=[
                f"① 先止血：把 {name} 连接池由 {pool} 临时调到 {pool * 2}，滚动重启（不要直接重启 DB）。",
                "② `show processlist` 找 Running 超过 60s 的查询 kill（只 kill 查询不 kill 连接）。",
                "③ 确认是慢查询 → 按 rb-mysql-slow-query 加索引或改写。",
                "④ 仍打满 → 按泄漏处理：抓线程 dump 定位未归还连接的调用栈。",
                "⑤ 把池大小与副本数联动计算纳入容量评审，写进 spec-" + key + "。",
            ],
            rollback="池参数回滚并滚动重启即可，无数据影响。",
            related=common_related + ["alert-p1-db-conn"],
            escalate="15 分钟未恢复 -> DBA 组 + 业务域值班双升级；涉资金链路立即升级。",
            mttr_target="15 分钟",
        )

    if fm_key == "mq-backlog":
        lag = (v + 1) * 5000
        return dict(
            key=rb_id, title=f"{name} 消费积压处置",
            severity="P2", owner=svc["oncall"],
            symptom=(
                f"{name} 消费组 lag 持续增长突破 {lag} 条且 10 分钟内无回落，"
                f"下游处理延迟从秒级涨到 {5 + v % 10} 分钟以上，业务方开始投诉数据不及时。"),
            impact=f"消息时效性下降；积压超过保留期（7 天）会丢消息，属于隐性资损风险。",
            triage=[
                ("消费组 lag 监控", f"确认积压量（当前 > {lag}）与增长斜率"),
                ("消费者实例数与分区数", "消费者数 < 分区数 → 并行度不足"),
                ("单条消费耗时分布", "单条耗时突增 → 下游依赖变慢，而非消费能力不足"),
            ],
            causes=[
                "消费者实例数少于分区数，并行度不足",
                "单条消息处理慢（下游 RPC / DB 瓶颈）",
                "生产端流量突增（上游批量任务 / 重试风暴）",
                "毒消息阻塞：某条消息反复失败导致分区停滞",
            ],
            steps=[
                f"① 先扩消费者：把 {name} 消费者从 {max(2, svc['replicas'] - 1)} 扩到分区数上限，并行度对齐。",
                "② 观察单条耗时：下游慢则先治下游，不要盲目扩消费者。",
                "③ 疑似毒消息：定位分区与 offset，跳过并转入死信人工处理。",
                "④ 历史积压可开启批量追赶模式（限速，避免打垮下游），见 spec-" + key + " 的追赶限速值。",
                "⑤ 恢复后核对端到端延迟曲线回落到基线。",
            ],
            rollback="消费者扩容缩回即回滚；追赶模式关闭即恢复原速。",
            related=common_related + ["alert-p1-mq-lag"],
            escalate="积压超过消息保留期 50% -> 升级消息域负责人评估丢数据风险。",
            mttr_target="30 分钟",
        )

    if fm_key == "cache-penetration":
        qps = (v + 1) * 300
        return dict(
            key=rb_id, title=f"{name} 缓存穿透压垮下游处置",
            severity="P1", owner=svc["oncall"],
            symptom=(
                f"{name} 缓存命中率从 99% 跌到 {60 + v % 15}%，DB QPS 从 {qps // 10} 涨到 {qps}，"
                f"DB CPU 超过 90%；日志出现大量查不到数据的空回源，错误码 {code}-500-001 增多。"),
            impact=f"DB 压力放大 {qps // 10} 倍以上，可能拖垮共享实例；接口 P99 随之劣化。",
            triage=[
                ("缓存命中率曲线", f"命中率跌破 80%（当前 {60 + v % 15}%）即异常"),
                ("DB 侧空查询占比", "空结果占比高 → 穿透；热点 key 失效 → 击穿"),
                ("query 来源分布", "同一非法参数高频出现 → 恶意或代码缺陷"),
            ],
            causes=[
                "恶意请求伪造不存在的 key，绕过缓存直击 DB",
                "批量 key 同时过期（缓存雪崩的近亲）",
                "空结果未缓存，每次都回源",
                "缓存实例抖动导致批量失效",
            ],
            steps=[
                f"① 先止血：在 {name} 入口对空结果缓存 60 秒（空值缓存），并开启参数合法性校验。",
                "② 布隆过滤器拦截非法 key（若该服务已接入，确认过滤器版本与数据一致性）。",
                "③ 对热点 key 改逻辑过期 + 互斥回源，避免同时失效。",
                "④ DB 压力仍高 → 对该接口临时限流，见 spec-" + key + " 的兜底限流值。",
                "⑤ 复盘：把空值缓存与布隆过滤器写入上线检查单。",
            ],
            rollback="空值缓存 TTL 到期自动恢复；限流规则删除即回滚。",
            related=common_related + ["rb-cache-avalanche", "alert-p1-db-conn"],
            escalate="DB被打垮影响邻居服务 → 立即升级 DBA 与平台组件值班。",
            mttr_target="15 分钟",
        )

    if fm_key == "threadpool":
        core = 50 + (v % 3) * 50
        return dict(
            key=rb_id, title=f"{name} 工作线程池打满处置",
            severity="P1", owner=svc["oncall"],
            symptom=(
                f"{name} 线程池活跃线程长期等于最大值（core {core} / max {core * 2}），队列积压 {core * 10} 上限打满，"
                f"新请求被拒绝返回 {code}-503-002，P99 飙升且 CPU 利用率并不高。"),
            impact=f"{name} 拒绝新请求；排队中的请求超时失败，调用方出现连锁超时。",
            triage=[
                ("线程池指标 active/queue", f"active={core * 2} 且队列打满即确认"),
                ("线程 dump 看线程在等什么", ("`jstack <pid>` 看 WAITING 状态分布" if is_java else "pprof goroutine dump 看阻塞点")),
                ("下游依赖耗时", "某个下游变慢是最常见根因"),
            ],
            causes=[
                "下游依赖变慢，线程被同步等待占住（最常见）",
                "池参数与流量模型不匹配（IO 密集型配了 CPU 密集型参数）",
                "重试风暴放大并发",
                "同步调用混入了本应异步的重活",
            ],
            steps=[
                f"① 先止血：对 {name} 触发源接口限流，阻止并发继续上涨。",
                "② dump 定位等待点：确认卡在哪个下游哪一类调用。",
                "③ 下游恢复后观察队列水位回落；不回落则临时扩副本分摊流量。",
                f"④ 调参需谨慎：把池参数修正为 IO 密集型配置（如 {core * 2} → {core * 4}），并压测验证。",
                "⑤ 复盘：为该池补充队列水位告警（见 alert-" + key + "-lat 的队列阈值）。",
            ],
            rollback="限流与池参数回滚即可，无数据影响。",
            related=common_related + ["alert-p1-threadpool"],
            escalate="15 分钟未恢复 -> 升级研发负责人；入口级影响升级 P1 事故响应。",
            mttr_target="15 分钟",
        )

    if fm_key == "slow-query":
        return dict(
            key=rb_id, title=f"{name} 慢查询拖垮数据库处置",
            severity="P1", owner=svc["oncall"],
            symptom=(
                f"{name} 所属 DB 实例 CPU 持续 90%+，slow_query_log 出现大量执行超过 {1 + v % 3}s 的 SQL，"
                f"上游 P95 从 {200 + v * 20}ms 涨到 2s+，并伴随连接池告警。"),
            impact=f"{name} 及共享该实例的邻居服务整体变慢，可能级联超时。",
            triage=[
                ("`pt-query-digest slow.log --limit 5`", "按总耗时取 Top5，确认是否新增 SQL"),
                ("`show full processlist`", "State=executing 且 Time 大的查询即嫌疑"),
                ("SQL 指纹数量对比 24h", "指纹突增 → 新版本发布引入"),
            ],
            causes=[
                "新版本发布引入未走索引的查询（最常见）",
                "数据量增长导致索引选择性下降",
                "统计信息过期，优化器选错计划",
                "批量任务在业务高峰执行",
            ],
            steps=[
                f"① 先止血：kill 掉最耗时的查询，并对 {name} 相关接口临时限流。",
                "② `EXPLAIN` 分析慢 SQL，确认是否走索引；缺失索引走 pol-db-change 变更流程。",
                "③ 索引上线后观察 5 分钟，确认 P95 回落。",
                "④ 无法立即加索引 → 把该查询切到只读从库（评估主从延迟）。",
                "⑤ 把该 SQL 指纹加入慢查询基线告警。",
            ],
            rollback="索引回滚用 DROP INDEX（确认无依赖后）；SQL 改写回退应用版本。",
            related=common_related + ["alert-p1-slow-sql", "pol-db-change"],
            escalate="15 分钟未缓解 -> DBA 组升级；涉资金库直接升级域负责人。",
            mttr_target="20 分钟",
        )

    # disk-full
    return dict(
        key=rb_id, title=f"{name} 磁盘水位告急处置",
        severity="P2", owner=svc["oncall"],
        symptom=(
            f"{name} 所在节点磁盘使用率超过 {85 + v % 5}%，日志写入开始报 No space left on device，"
            f"应用日志滚动异常，Pod 可能因探针失败被重启。"),
        impact=f"日志与临时文件无法写入；磁盘打满会导致进程崩溃，影响 {name} 可用性。",
        triage=[
            ("`kubectl exec <pod> -- df -h`", "确认使用率（当前 > " + str(85 + v % 5) + "%）与挂载点"),
            ("`du -sh /data/logs/* | sort -h | tail`", "找出占用最大的日志 / 临时文件"),
            ("日志采集器状态", "采集停摆会让日志无限堆积，是常见根因"),
        ],
        causes=[
            "日志采集器停摆导致日志堆积（最常见）",
            "临时文件 / dump 文件未清理",
            "日志滚动策略未按预期生效（按天应为按大小）",
            "节点本地盘容量与新版本日志量不匹配",
        ],
        steps=[
            f"① 先止血：对 {name} 所在节点清理安全目标（已滚动的历史日志、超过 3 天的 dump）。",
            "② 恢复日志采集器，确认采集位点重新推进。",
            "③ 验证应用写日志不再报错，探针恢复。",
            "④ 把该节点的日志滚动策略改为按大小（如 100MB × 10 份）。",
            "⑤ 恢复后观察磁盘水位回落到 70% 以下再收尾。",
        ],
        rollback="清理动作只针对历史文件，无需回滚；滚动策略变更可随时改回。",
        related=common_related + ["rb-inode-exhaust", "alert-p1-disk-usage"],
        escalate="多节点同时告急 → 升级平台组件值班排查采集链路。",
        mttr_target="20 分钟",
    )


# =====================================================================
# 四、告警 / FAQ / 命令 / 可观测 / 长文档 派生
# =====================================================================

def _fleet_alerts(svc: dict, i: int) -> list[dict]:
    key, name = svc["key"], svc["name"]
    code = _FLEET_TABLE[i][7]
    lat_thr = (500 + (i % 5) * 100)      # P99 延迟阈值 ms：500~900
    err_thr = 1 + (i % 4)                # 错误率阈值 %：1~4
    cpu_thr = 70 + (i % 4) * 5           # CPU 阈值：70~85%
    mem_thr = 75 + (i % 4) * 5           # 内存阈值：75~90%
    fm0 = f"rb-{key}-{_FLEET_FAILMODES[i % 8][0]}"
    return [
        dict(
            key=f"alert-{key}-lat", title=f"{name} 延迟与错误率告警（P1）", category="alert",
            severity="P1", tags=[key, "可用性"],
            summary=f"{name} P99 超过 {lat_thr}ms 持续 5 分钟，或 5xx 错误率超过 {err_thr}% 持续 3 分钟，触发企微 + 电话告警。",
            sections=[
                ["告警表达式",
                 "```promql\nhistogram_quantile(0.99, sum(rate(" + key + "_request_duration_seconds_bucket[5m])) by (le)) > "
                 + str(lat_thr / 1000) + "\n```"],
                ["触发条件", f"P99 > {lat_thr}ms 持续 5m；或 5xx 错误率 > {err_thr}% 持续 3m"],
                ["通知渠道", f"电话 + 企微 P1 群（{svc['oncall']}）"],
                ["处置入口", f"按错误码分流：{code}-503-001 多为依赖问题（见 {fm0}）；{code}-500-001 多为本服务问题（见 cmd-{key}）"],
                ["常见误报", "发布窗口滚动重启会产生短时抖动，发布期抑制窗口 3 分钟（见 pol-gray-release）"],
            ],
            related=[f"svc-{key}", fm0],
        ),
        dict(
            key=f"alert-{key}-res", title=f"{name} 资源水位告警（P2）", category="alert",
            severity="P2", tags=[key, "容量"],
            summary=f"{name} 容器 CPU 超过 {cpu_thr}% 或内存超过 {mem_thr}% 持续 10 分钟，触发企微告警（不打电话）。",
            sections=[
                ["告警表达式",
                 "```promql\ncontainer_cpu_usage_percent{pod=~\"" + key + "-.*\"} > " + str(cpu_thr) + "\n```"],
                ["触发条件", f"CPU > {cpu_thr}% 或内存 > {mem_thr}%，持续 10m"],
                ["通知渠道", f"企微值班群（{svc['oncall']}）"],
                ["处置入口", f"内存水位贴顶优先按 {fm0} 处置（本服务首轮处置手册）；CPU 高优先看线程池与下游耗时"],
                ["常见误报", "批量任务窗口的资源尖峰，已在 spec-{k} 中约定低峰执行".format(k=key)],
            ],
            related=[f"svc-{key}", fm0],
        ),
    ]


def _fleet_faqs(svc: dict, i: int) -> list[dict]:
    key, name, domain = svc["key"], svc["name"], svc["domain"]
    code = _FLEET_TABLE[i][7]
    qlimit = 20 + (i % 6) * 10
    api_limit = (2 + (i % 4)) * 1000
    codes = _ERROR_ROWS
    fm0 = f"rb-{key}-{_FLEET_FAILMODES[i % 8][0]}"
    return [
        dict(
            key=f"faq-{key}-01", title=f"{name} 是负责什么的？出问题找谁？", category="faq",
            tags=["FAQ", domain],
            summary=f"{name}（`{key}`）属于{domain}，值班组 {svc['oncall']}，出问题按严重程度走 oncall-escalation 升级。",
            sections=[
                ["定位", f"{svc['note']}"],
                ["值班与升级", f"日常问题联系 {svc['oncall']}；15 分钟无响应或影响面扩大，按 oncall-escalation 升级到域负责人。"],
                ["快速自检", f"健康检查 {svc['health']}；指标端口 {svc['port'].split(' / ')[1]}；常用命令见 cmd-{key}。"],
            ],
            related=[f"svc-{key}", "oncall-escalation", f"cmd-{key}"],
        ),
        dict(
            key=f"faq-{key}-02", title=f"{name} 常见错误码有哪些？怎么处理？", category="faq",
            tags=["FAQ", "错误码"],
            summary=f"{name} 的错误码前缀是 {code}，最高频的三类：{code}-429-001（限流）、{code}-503-001（依赖不可用）、{code}-500-001（内部错误）。",
            sections=[
                [f"{code}-429-001", codes["429"][0] + "；" + codes["429"][3] + "。"],
                [f"{code}-503-001", codes["503"][0] + "；" + codes["503"][3] + f"，常见根因见 {fm0}。"],
                [f"{code}-500-001", codes["500"][0] + "；" + codes["500"][3] + f"。完整字典见 err-{key}。"],
            ],
            related=[f"err-{key}", f"svc-{key}", f"cmd-{key}"],
        ),
        dict(
            key=f"faq-{key}-03", title=f"{name} 的限流与配额是多少？", category="faq",
            tags=["FAQ", "限流"],
            summary=f"{name} 单用户操作 QPS 限 {qlimit}，单接口限流 {api_limit} QPS，超限返回 {code}-429-001 / {code}-429-002。",
            sections=[
                ["单用户限流", _LIMIT_TEXT.format(qlimit=qlimit, code=code) + "。"],
                ["单接口限流", f"{api_limit} QPS，超限返回 {code}-429-002，429 必须按 Retry-After 退避。"],
                ["容量基线", f"压测基线与扩容阈值见 spec-{key}；大促前预热流程见 pol-capacity-preheat。"],
            ],
            related=[f"svc-{key}", f"spec-{key}", "pol-capacity-preheat"],
        ),
    ]


def _fleet_cmd(svc: dict, i: int) -> dict:
    key, name = svc["key"], svc["name"]
    port = _FLEET_TABLE[i][4]
    is_java = "Java" in svc["stack"]
    return dict(
        key=f"cmd-{key}", title=f"{name} 排查命令速查", category="cmd",
        tags=[key, "命令"],
        summary=f"{name}（`{key}`）日常排查的标准命令序列：健康、日志、指标、依赖。",
        sections=[
            ["健康与状态",
             "```bash\n"
             f"kubectl get pods -l app={key} -o wide\n"
             f"curl -s http://localhost:{port}{('/actuator/health' if is_java else '/healthz')} | head\n"
             "```"],
            ["日志",
             "```bash\n"
             f"kubectl logs -l app={key} --tail=200 --prefix\n"
             "kubectl logs <pod> --previous   # 崩溃前的最后日志\n"
             "```"],
            ["指标",
             f"指标端口 {port + 1000}，抓取路径 /metrics；核心指标 `{key}_requests_total` 与 `{key}_request_duration_seconds`，面板见 obs-{key}。"],
        ],
        related=[f"svc-{key}", f"obs-{key}", "cmd-kubectl"],
    )


def _fleet_obs(svc: dict, i: int) -> dict:
    key, name = svc["key"], svc["name"]
    lat_thr = (500 + (i % 5) * 100) // 1000
    return dict(
        key=f"obs-{key}", title=f"{name} 可观测指标说明", category="obs",
        tags=["可观测", key],
        summary=f"{name} 遵循 RED 方法：{key}_requests_total（速率）、{key}_request_duration_seconds（耗时）、{key}_errors_total（错误）。",
        sections=[
            ["指标命名", f"指标名以服务名 {key} 为前缀，命名规范见 obs-metric-naming；禁止在指标名里塞高基数维度。"],
            ["黄金面板", f"Grafana 面板 `ops / {key} / golden`：QPS、P99（阈值 {lat_thr}s 量级告警见 alert-{key}-lat）、错误率、副本数四联图。"],
            ["链路与日志", f"trace 采样走 obs-sampling 规则；日志强制带 trace_id（见 obs-log-spec），排障先按 trace_id 串联。"],
        ],
        related=[f"svc-{key}", f"alert-{key}-lat", "obs-metric-naming", "obs-sampling"],
    )


def _fleet_guide(svc: dict, i: int) -> dict:
    key, name = svc["key"], svc["name"]
    return dict(
        key=f"guide-{key}", title=f"{name} 上线部署指南", category="guide",
        tags=["部署", key],
        summary=f"{name} 的标准上线序列：预检、灰度、全量、观察，全程约 25 分钟；任何一步失败按 pol-rollback 回滚。",
        sections=[
            {"h": "预检（5 分钟）",
             "p": f"① 确认当前副本 {svc['replicas']} 全部 Ready；② 检查上游依赖健康（{svc['health']}）；③ 确认无进行中的批量任务（infra-job 窗口）；④ 发布单已过 pol-gray-release 评审。"},
            {"h": "灰度（10 分钟）",
             "p": f"先发 1 个副本观察 5 分钟：P99、错误率、{key}_errors_total 三项无恶化再继续；灰度期间 alert-{key}-lat 的发布抑制窗口生效（3 分钟）。"},
            {"h": "全量与观察（10 分钟）",
             "p": "滚动替换剩余副本，每批间隔 2 分钟；全量后观察 10 分钟，重点看错误率曲线回落到基线。"},
            {"h": "回滚预案",
             "p": f"任一指标恶化立即执行 pol-rollback：回退镜像版本 + 滚动重启，5 分钟内完成；回滚后禁止立刻重试发布，先复盘。"},
        ],
        related=[f"svc-{key}", "pol-gray-release", "pol-rollback", f"alert-{key}-lat"],
    )


def _fleet_spec(svc: dict, i: int) -> dict:
    key, name = svc["key"], svc["name"]
    api_limit = (2 + (i % 4)) * 1000
    base_qps = api_limit * 3
    fm0 = f"rb-{key}-{_FLEET_FAILMODES[i % 8][0]}"
    return dict(
        key=f"spec-{key}", title=f"{name} 容量与配额规范", category="spec",
        tags=["容量", key],
        summary=f"{name} 压测基线 {base_qps} QPS（P99 达标），单接口限流 {api_limit} QPS；扩容触发线 65%，缩容回撤线 30%。",
        sections=[
            {"h": "压测基线",
             "p": f"基线流量 {base_qps} QPS 持续 30 分钟：P99 达标（{svc['slo_latency']}）、错误率 < 0.1%、无内存持续上涨。基线每季度重测，发布重大变更后必须复测。"},
            {"h": "扩缩容策略",
             "p": f"HPA 触发线：CPU 65% 扩容、30% 回撤；副本区间 2~{svc['replicas'] * 2}。扩容后必须核对连接池与副本数联动（见 {fm0} 与全局连接池处置 rb-mysql-conn-pool 的教训）。"},
            {"h": "大促保障",
             "p": f"大促前按 pol-capacity-preheat 预热：缓存预 hit、连接池预扩、限流阈值复核；高峰期禁止一切非紧急变更（pol-config-change）。"},
            {"h": "配额红线",
             "p": f"单接口硬上限 {api_limit} QPS（网关层强制），DB 连接池与副本数联动计算；突破红线需容量评审，见 arch-capacity。"},
        ],
        related=[f"svc-{key}", "arch-capacity", "pol-capacity-preheat", fm0],
    )


def _fleet_pm(svc: dict, i: int) -> dict:
    key, name = svc["key"], svc["name"]
    fm_key, fm_title, _sev = _FLEET_FAILMODES[i % 8]
    day = 3 + (i % 25)
    month = 3 + (i % 6)
    dur = 12 + (i % 40)
    rb0 = f"rb-{key}-{fm_key}"
    return dict(
        key=f"pm-{key}", title=f"{name} {fm_title}故障复盘（2026-{month:02d}）", category="postmortem",
        tags=["复盘", key],
        summary=(
            f"2026-{month:02d}-{day:02d} {name} 因 {fm_title} 不可用 {dur} 分钟。"
            f"本文还原时间线与根因，并沉淀 {3 + i % 4} 项改进措施；处置手册见 {rb0}。"),
        sections=[
            {"h": "事件概要",
             "p": (f"| 项目 | 内容 |\n| --- | --- |\n| 事件等级 | P{'1' if _FLEET_FAILMODES[i % 8][2] == 'P1' else '2'} |\n"
                   f"| 影响服务 | `svc-{key}` |\n| 开始时间 | 2026-{month:02d}-{day:02d} 10:{(i % 50) + 10:02d} |\n"
                   f"| 持续时间 | {dur} 分钟 |\n| 根因 | {fm_title}，详见下文 |\n| 是否可预防 | 是 |")},
            {"h": "时间线",
             "p": (f"10:{(i % 50) + 10:02d} 监控告警触发（alert-{key}-lat）；"
                   f"+3 分钟值班响应并按 {rb0} 开始处置；+{dur - 5} 分钟止血生效；+{dur} 分钟恢复确认。"
                   "检测到恢复全程依赖监控而非用户反馈，说明告警覆盖有效。")},
            {"h": "根因分析",
             "p": f"直接根因是 {fm_title}；深层原因是容量参数与流量模型脱节，变更缺少联动评审。同类全局知识见 {_GLOBAL_RB[fm_key]}。"},
            {"h": "改进措施",
             "p": (f"① 容量参数（连接池 / 副本 / 超时）纳入统一容量评审（见 spec-{key}）；"
                   f"② {rb0} 补充自动化判定命令，减少人工判读；"
                   f"③ 告警阈值按本复盘校准（alert-{key}-lat）；"
                   "④ 该类故障加入季度演练清单（oncall-drill）。")},
        ],
        related=[f"svc-{key}", rb0, f"alert-{key}-lat", "oncall-postmortem"],
    )


# =====================================================================
# 五、评估标注集派生（与文档同源，锚点逐字取自上方模板常量）
# =====================================================================

def fleet_questions(start_id: int) -> list[dict]:
    """生成舰队覆盖的评估题（gen_eval 内部 schema：qtype / image_ref 由落盘层转 notes）。

    五维检索覆盖（single-hop / multi-hop / colloquial / edge / multimodal），
    锚点全部来自文档正文的字面量 —— check_annotation 第 8 条可机器验证。
    golden 只引用舰队必然生成的 id（svc/err/alert/rb 首选故障模式），
    不引用"可能没被轮转选中"的模板 id。
    """
    qs: list[dict] = []
    qid = start_id

    def add(intent, difficulty, question, golden, anchors, points, qtype, image_ref=None):
        nonlocal qid
        q = dict(id=qid, intent=intent, difficulty=difficulty, question=question,
                 golden_doc_ids=golden, answer_anchors=anchors,
                 key_points=points, qtype=qtype)
        if image_ref:
            q["image_ref"] = image_ref
        qs.append(q)
        qid += 1

    for i, row in enumerate(_FLEET_TABLE):
        key, name, domain = row[0], row[1], row[2]
        code = row[7]
        qlimit = 20 + (i % 6) * 10
        api_limit = (2 + (i % 4)) * 1000
        lat_thr = 500 + (i % 5) * 100
        err_thr = 1 + (i % 4)
        cpu_thr = 70 + (i % 4) * 5
        mem_thr = 75 + (i % 4) * 5
        fm0 = _FLEET_FAILMODES[i % 8][0]
        rb0 = f"rb-{key}-{fm0}"
        oncall = row[8]

        # single-hop：限流与错误码（每服务 1 题，共 60）
        add("rag", "easy",
            f"「{name}」的单用户操作限流是多少？超限返回什么错误码？",
            [f"svc-{key}", f"err-{key}"],
            [f"{code}-429-001", str(qlimit)],
            [f"单用户操作 QPS 限 {qlimit}", f"超限返回 {code}-429-001", "按 Retry-After 退避重试"],
            "single-hop")

        # multi-hop：告警阈值 → 处置手册（隔一个服务 1 题，共 30）
        if i % 2 == 0:
            add("rag", "medium",
                f"「{name}」的 P1 延迟告警阈值是多少？告警触发后如果 5xx 集中在 {code}-503-001，应该按哪篇文档处置？",
                [f"alert-{key}-lat", rb0],
                [str(lat_thr), str(err_thr), f"{code}-503-001"],
                [f"P99 > {lat_thr}ms 持续 5 分钟触发", f"错误率 > {err_thr}% 持续 3 分钟",
                 f"{code}-503-001 集中 → 依赖问题，按 {rb0} 处置"],
                "multi-hop")

        # colloquial：口语化找负责人（每 3 个服务 1 题，共 20）
        if i % 3 == 0:
            add("rag", "easy",
                f"线上有人反馈 {name} 挂了，大半夜的应该喊谁？这个服务具体是干嘛的？",
                [f"faq-{key}-01", f"svc-{key}"],
                [oncall, domain],
                [f"值班组是 {oncall}", f"{name} 属于{domain}", "15 分钟无响应按 oncall-escalation 升级"],
                "colloquial")

        # edge：容量红线与联动调整（每 4 个服务 1 题，共 15）
        if i % 4 == 0:
            add("rag", "hard",
                f"「{name}」的压测基线是多少？连接池和副本数为什么要联动调整？",
                [f"spec-{key}", rb0],
                [str(api_limit * 3), str(api_limit)],
                [f"压测基线 {api_limit * 3} QPS", f"单接口限流 {api_limit} QPS",
                 "扩容后连接池不联动会耗尽，见连接池耗尽处置"],
                "edge")

        # multimodal：监控面板截图对应（每 6 个服务 1 题，共 10）
        if i % 6 == 0:
            add("rag", "medium",
                f"监控面板截图显示 {name} 的 CPU 与内存水位双双超过告警阈值，应该按哪篇告警规则确认阈值、按哪篇处置手册处理？",
                [f"alert-{key}-res", rb0],
                [str(cpu_thr), str(mem_thr)],
                [f"CPU 告警阈值 {cpu_thr}%", f"内存告警阈值 {mem_thr}%",
                 f"资源贴顶按 {rb0} 处置"],
                "multimodal", image_ref=f"img-{key}-panel")

    return qs


# =====================================================================
# 六、装配入口
# =====================================================================

_FLEET_SERVICES: list[dict] | None = None


def fleet_services() -> list[dict]:
    global _FLEET_SERVICES
    if _FLEET_SERVICES is None:
        _FLEET_SERVICES = _build_fleet_services()
    return _FLEET_SERVICES


def fleet_error_codes() -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for i, svc in enumerate(fleet_services()):
        out[svc["key"]] = _build_fleet_error_codes(svc, i)
    return out


def fleet_runbooks() -> list[dict]:
    out: list[dict] = []
    for i, svc in enumerate(fleet_services()):
        for pk in sorted({_FLEET_FAILMODES[i % 8][0], _FLEET_FAILMODES[(i + 3) % 8][0],
                          _FLEET_FAILMODES[(i + 5) % 8][0]}):
            out.append(_failmode_runbook(svc, i, pk))
    return out


def fleet_alerts() -> list[dict]:
    out: list[dict] = []
    for i, svc in enumerate(fleet_services()):
        out.extend(_fleet_alerts(svc, i))
    return out


def fleet_faqs() -> list[dict]:
    out: list[dict] = []
    for i, svc in enumerate(fleet_services()):
        out.extend(_fleet_faqs(svc, i))
    return out


def fleet_commands() -> list[dict]:
    return [_fleet_cmd(svc, i) for i, svc in enumerate(fleet_services())]


def fleet_obs() -> list[dict]:
    return [_fleet_obs(svc, i) for i, svc in enumerate(fleet_services())]


def fleet_longform() -> list[dict]:
    out: list[dict] = []
    for i, svc in enumerate(fleet_services()):
        out.append(_fleet_guide(svc, i))
        out.append(_fleet_spec(svc, i))
        out.append(_fleet_pm(svc, i))
    return out


def apply() -> None:
    """把手写源列表原位扩展。由 data/source/__init__.py 调用一次且仅一次。"""
    from data.source.services import SERVICES, ERROR_CODES
    from data.source.runbooks import RUNBOOKS
    from data.source.policies_arch import ALERTS
    from data.source.operations import COMMANDS, FAQ, OBSERVABILITY
    from data.source.longform import LONGFORM_DOCS

    fs = fleet_services()
    SERVICES.extend(fs)
    ERROR_CODES.update(fleet_error_codes())
    RUNBOOKS.extend(fleet_runbooks())
    ALERTS.extend(fleet_alerts())
    FAQ.extend(fleet_faqs())
    COMMANDS.extend(fleet_commands())
    OBSERVABILITY.extend(fleet_obs())
    LONGFORM_DOCS.extend(fleet_longform())


def counts() -> dict[str, int]:
    return {
        "services": len(fleet_services()),
        "errors": len(fleet_error_codes()),
        "runbooks": len(fleet_runbooks()),
        "alerts": len(fleet_alerts()),
        "faqs": len(fleet_faqs()) * 1,
        "commands": len(fleet_commands()),
        "obs": len(fleet_obs()),
        "longform": len(fleet_longform()),
    }
