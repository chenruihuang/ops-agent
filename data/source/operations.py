"""值班流程 / 可观测规范 / 命令手册 / FAQ —— 知识源（续）。"""

from __future__ import annotations

# =====================================================================
# 四、值班与流程（7 篇）
# =====================================================================

ONCALL: list[dict] = [
    dict(
        key="oncall-duty",
        title="值班手册（On-call Handbook）",
        category="oncall",
        tags=["值班", "流程"],
        summary="值班职责、响应时限与标准动作。核心原则：先恢复服务，再定位根因。",
        sections=[
            ("值班分组", "@infra-oncall（基础架构）、@trade-oncall（交易域）、@user-oncall（用户域）、"
                     "@pay-oncall（支付域，7x24）、@search-oncall（搜索域）、@mkt-oncall（营销域）、"
                     "@data-oncall（数据域）、@risk-oncall（风控域）。"),
            ("响应时限", "P0：5 分钟内响应，电话通知；P1：15 分钟内响应，企微通知；P2：当日处理；P3：排期处理。"),
            ("标准动作", "① 确认告警并回复「已认领」→ ② 判断影响面 → ③ 按对应 Runbook 处置 → "
                     "④ 恢复后记录时间线 → ⑤ 24 小时内发起复盘。"),
            ("工具入口", "告警：企微机器人 + 电话；日志：Kibana；指标：Grafana；链路：Langfuse / SkyWalking；"
                     "部署：ArgoCD；工单：ticket_sys（可通过 ops-agent 查询）。"),
            ("交接要求", "每班次结束必须发送交接消息，包含未闭环告警、进行中的变更、待观察项，格式见 oncall-shift。"),
            ("关联", "事故分级标准见 oncall-incident-level；升级路径见 oncall-escalation。"),
        ],
        related=["oncall-incident-level", "oncall-escalation", "oncall-shift", "oncall-postmortem"],
    ),
    dict(
        key="oncall-incident-level",
        title="事故分级标准",
        category="oncall",
        tags=["事故", "流程"],
        summary="事故分 P0-P3 四级，分级决定响应时限、参与范围与升级路径。",
        sections=[
            ("P0 事故", "核心交易链路不可用，或存在资金 / 数据错误风险。例：网关 5xx > 5%、支付回调积压、缓存雪崩、ES red。"
                      "要求：立即电话召集，全员响应，每 15 分钟同步进展。"),
            ("P1 事故", "核心功能降级或部分用户不可用。例：单服务 Pod 频繁重启、单库连接池耗尽、MQ 积压、磁盘满。"
                      "要求：值班 15 分钟内响应，必要时升级。"),
            ("P2 事故", "非核心功能异常或性能下降，有临时绕过方案。要求：当日处理。"),
            ("P3 事故", "体验问题或告警噪音。要求：排期处理。"),
            ("升级规则", "P1 超过 30 分钟未缓解自动升级为 P0；任一事故影响范围扩大（跨域）自动升级一级。"),
            ("关联", "P0 的典型处置路径见 rb-full-link-timeout、rb-pay-callback-timeout。"),
        ],
        related=["oncall-duty", "rb-full-link-timeout", "rb-pay-callback-timeout", "oncall-escalation"],
    ),
    dict(
        key="oncall-postmortem",
        title="事故复盘模板",
        category="oncall",
        tags=["复盘", "流程"],
        summary="复盘必须回答五个问题：发生了什么、为什么发生、为什么没早发现、如何恢复、如何防止再发生。",
        sections=[
            ("时间线", "以分钟为粒度记录：T1 故障发生 / T2 告警触发 / T3 人工响应 / T4 止血动作 / T5 完全恢复 / T6 复盘完成。"),
            ("核心指标", "MTTD（检测时间）= T2-T1；MTTR（恢复时间）= T5-T2；MTTF（止血前耗时）= T4-T2。"),
            ("根因分析", "区分直接原因、根本原因与系统性原因。例：直接原因是慢 SQL，根本原因是新版本未做 SQL 评审，"
                      "系统性原因是缺少发布前的 SQL 指纹基线对比。"),
            ("行动项", "每条行动项必须有责任人与截止时间，并标注类型（监控 / 流程 / 代码 / 文档）。"
                     "禁止出现「加强关注」这类不可执行项。"),
            ("案例参考", "风控 fail-open 策略、报表任务重复执行、证书过期致 P0 —— 这三个是历史上的典型事故，见 svc-risk-engine、rb-cron-duplicate、rb-cert-expired。"),
        ],
        related=["oncall-incident-level", "rb-cron-duplicate", "rb-cert-expired", "svc-risk-engine"],
    ),
    dict(
        key="oncall-node-replace",
        title="K8s 节点替换流程",
        category="oncall",
        tags=["k8s", "运维"],
        summary="节点硬件故障后的标准替换流程：驱逐 → 摘除 → 替换 → 重新加入。",
        sections=[
            ("触发条件", "节点硬件故障、内核 panic、磁盘物理损坏，或 NotReady 且无法恢复（见 rb-node-notready）。"),
            ("驱逐", "`kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`，注意确认没有不可中断的裸 Pod。"),
            ("摘除", "`kubectl delete node <node>` 并从 CMDB 与监控目标中移除。"),
            ("替换", "按节点模板重建（云上直接创建同规格实例并加入集群）。"),
            ("回归验证", "`kubectl uncordon <node>`，确认节点 Ready、DaemonSet 正常、监控指标上报恢复。"),
            ("关联", "节点故障的初步处置见 rb-node-notready；容灾设计见 arch-multi-az。"),
        ],
        related=["rb-node-notready", "arch-multi-az"],
    ),
    dict(
        key="oncall-shift",
        title="交接班规范",
        category="oncall",
        tags=["值班", "流程"],
        summary="交接班必须书面化，未闭环事项必须明确责任人，禁止口头交接。",
        sections=[
            ("交接内容", "① 当前未闭环的告警与事故（含最新进展）② 进行中的变更与发布 ③ 待观察项 ④ 临时绕过手段。"),
            ("交接消息格式", "`【交接】日期 班次 | 未闭环：N 项 | 进行中变更：N 项 | 待观察：N 项 | 备注：...`，发到值班群。"),
            ("高风险提示", "若存在临时手段（如手工 hosts、临时扩容、关闭的告警），必须醒目标注并要求回滚计划。"),
            ("交接受限", "若接班人不明确，交班人不得离线；P0 事故期间禁止交接。"),
        ],
        related=["oncall-duty", "oncall-incident-level"],
    ),
    dict(
        key="oncall-drill",
        title="故障演练规范",
        category="oncall",
        tags=["演练", "高可用"],
        summary="每季度至少一次故障演练，目标是把「第一次遇到」变成「演练过」。",
        sections=[
            ("演练类型", "单 AZ 故障、单依赖不可用（Redis / MySQL / 向量库）、依赖限流（LLM 429）、磁盘写满。"),
            ("演练要求", "必须真实注入故障，禁止只走桌面推演；必须在业务低峰期进行并提前报备。"),
            ("验收指标", "在演练中测量：故障检出时间、降级生效时间、请求成功率、恢复时间（即 MTTD / MTTR 基线）。"),
            ("降级验证", "重点验证「依赖全挂时主链路是否仍可应答」——例如向量库不可用时，问答是否降级为无资料应答而不是直接 500。"),
            ("记录", "演练报告包含注入方式、实际观测值、与预期的差异、改进项，归档到知识库（即本语料库）。"),
        ],
        related=["arch-multi-az", "arch-circuit-breaker", "oncall-postmortem"],
    ),
    dict(
        key="oncall-escalation",
        title="升级路径与通知策略",
        category="oncall",
        tags=["值班", "升级"],
        summary="各级事故的升级路径、通知渠道与召集规则。",
        sections=[
            ("P0 升级", "值班 → 业务域负责人（5 分钟）→ 架构组（10 分钟）→ CTO（30 分钟）。渠道：电话 + P0 群。"),
            ("P1 升级", "值班 → 业务域负责人（15 分钟）→ 架构组（30 分钟）。渠道：企微。"),
            ("跨域故障", "任一事故影响两个以上业务域，立即升级为 P0 并召集全链路会议。示例：rb-full-link-timeout。"),
            ("资金相关", "涉及支付的任何事故（含 P2）都必须同步支付域负责人与财务，见 rb-pay-callback-timeout。"),
            ("通知纪律", "禁止在未确认影响面前大范围 @全员；升级消息必须包含「现象 + 影响面 + 已做动作 + 需要谁」。"),
        ],
        related=["oncall-incident-level", "rb-full-link-timeout", "rb-pay-callback-timeout", "oncall-duty"],
    ),
]


# =====================================================================
# 五、可观测规范（6 篇）
# =====================================================================

OBSERVABILITY: list[dict] = [
    dict(
        key="obs-log-spec",
        title="日志规范",
        category="obs",
        tags=["日志", "规范"],
        summary="日志必须结构化（JSON）、必须带 trace_id、必须分级明确，禁止输出明文敏感信息。",
        sections=[
            ("格式", "统一 JSON：`{ts, level, service, trace_id, span_id, msg, ...extra}`。禁止纯文本拼接。"),
            ("分级标准", "ERROR：需要人工介入（如 DB 连接失败）；WARN：可自愈或降级已生效（如缓存不可用已降级）；"
                       "INFO：关键业务节点（订单创建成功）；DEBUG：仅本地开启。"),
            ("禁止内容", "禁止打印手机号、身份证、银行卡、密码、Token；必须脱敏为 `138****1234`。"),
            ("采样", "ERROR 全量、INFO 按 10% 采样、DEBUG 关闭，采样策略见 obs-sampling。"),
            ("采集链路", "Filebeat → Kafka → Logstash → ES → Kibana，详见 obs-log-pipeline。"),
            ("关联", "链路追踪的字段约定见 obs-trace-spec。"),
        ],
        related=["obs-trace-spec", "obs-sampling", "obs-log-pipeline"],
    ),
    dict(
        key="obs-trace-spec",
        title="链路追踪规范",
        category="obs",
        tags=["trace", "规范"],
        summary="所有跨服务与跨系统调用必须透传 trace_id，LLM 调用额外透传 session_id 与 user_id。",
        sections=[
            ("透传要求", "trace_id 通过 HTTP header `X-Trace-Id` 与 Kafka message header 透传，缺失时由入口生成。"),
            ("Span 命名", "`<service>.<operation>`，例如 `order-service.createOrder`、`payment-gateway.channelCall`。"),
            ("LLM 相关", "LLM 调用额外记录 `session_id`、`model`、`prompt_version`、`token_in`、`token_out`、`cost`，"
                      "便于做成本归因与 A/B 对比。"),
            ("采样策略", "正常请求 10% 采样；错误请求 100%；P99 慢请求 100%。"),
            ("工具", "自建服务用 SkyWalking；LLM 链路用 Langfuse（见 obs-langfuse）。"),
            ("关联", "日志字段见 obs-log-spec；指标命名见 obs-metric-naming。"),
        ],
        related=["obs-log-spec", "obs-langfuse", "obs-metric-naming", "obs-sampling"],
    ),
    dict(
        key="obs-metric-naming",
        title="指标命名规范",
        category="obs",
        tags=["metrics", "规范"],
        summary="遵循 Prometheus 命名约定：`<namespace>_<subsystem>_<name>_<unit>`，单位必须显式。",
        sections=[
            ("命名格式", "`ops_agent_llm_latency_seconds`、`ops_agent_cache_hits_total`、`order_service_order_created_total`。"),
            ("单位后缀", "时间用 `_seconds`（不是 ms）；字节用 `_bytes`；总数用 `_total`；比率不带后缀（0-1）。"),
            ("标签纪律", "标签基数必须可控：禁止用 user_id、order_no 做标签（会导致时间线爆炸）。"),
            ("必备指标", "每个服务必须暴露：请求总数（按状态码）、耗时直方图、线程池 / 连接池水位、依赖调用耗时。"),
            ("黄金信号", "延迟、流量、错误、饱和度（USE + RED 方法），对应 Grafana 大盘的四个基础面板。"),
            ("关联", "告警规则基于这些指标，见 ALERTS 系列（如 alert-p1-threadpool）。"),
        ],
        related=["obs-trace-spec", "alert-p1-threadpool", "arch-sla"],
    ),
    dict(
        key="obs-log-pipeline",
        title="日志采集链路说明",
        category="obs",
        tags=["日志", "链路"],
        summary="日志采集链路：Filebeat（节点级）→ Kafka → Logstash（解析）→ ES（存储）→ Kibana（查询），保留 14 天。",
        sections=[
            ("采集", "Filebeat 以 DaemonSet 部署，采集宿主机 `/var/log/containers/*.log`。"),
            ("缓冲", "经 Kafka `log-topic`（12 分区）缓冲，避免 Logstash 故障导致日志丢失。"),
            ("解析", "Logstash 按服务解析 JSON，补充 service / env / namespace 字段。"),
            ("存储", "ES `app-log-*` 索引按天滚动，保留 14 天，冷数据转 MinIO（ILM 策略）。"),
            ("排查入口", "Kibana 按 `trace_id` 聚合，或在 Grafana 从指标面板下钻到 Kibana 日志。"),
            ("风险", "日志链路自身也占资源：ES 索引量过大时会触发 rb-disk-full，需关注 `app-log-*` 体积。"),
        ],
        related=["obs-log-spec", "rb-disk-full", "obs-sampling"],
    ),
    dict(
        key="obs-sampling",
        title="采样策略",
        category="obs",
        tags=["采样", "成本"],
        summary="三类采样：日志采样、链路采样、评估采样，原则是「错误全留、成功抽样、异常加权」。",
        sections=[
            ("日志采样", "ERROR / WARN 全量；INFO 10%；DEBUG 生产环境关闭（需通过开关临时开启，开启不超过 30 分钟）。"),
            ("链路采样", "正常 10%、错误 100%、慢请求（> P99）100%。"),
            ("评估采样", "构建评估集时，从线上真实问题中抽样：高频问题全取、长尾问题按难度分层抽样，避免只取简单问题导致指标虚高。"),
            ("成本换算", "采样率每降低 10 个百分点，日志存储成本下降约 10%，但排障效率下降更明显 —— 生产环境不建议低于 5%。"),
        ],
        related=["obs-log-spec", "obs-trace-spec", "arch-capacity"],
    ),
    dict(
        key="obs-langfuse",
        title="Langfuse 使用说明（LLM 链路可观测）",
        category="obs",
        tags=["LLM", "可观测"],
        summary="ops-agent 的 LLM 调用、检索、重排全部上报 Langfuse，用于链路追溯、成本归因与评估集管理。",
        sections=[
            ("部署", "docker compose 自托管，服务名 langfuse，端口 3000（Web UI）。"),
            ("上报内容", "每个请求一个 trace，包含：查询改写、召回文档 id 与分数、重排结果、prompt 版本、"
                      "模型名、token 用量、耗时、缓存命中来源。"),
            ("核心用途", "① 定位「答错了」是哪一环出的问题（召回没中 / 重排没排上来 / prompt 没说清）；"
                      "② 统计单次问答 token 成本；③ 管理评估数据集并记录每次实验分数。"),
            ("关键指标", "TTFT、首字到末字耗时、token_in / token_out、缓存命中率（exact / semantic / miss 三类占比）。"),
            ("与 Prometheus 分工", "Langfuse 管链路细节与成本；Prometheus 管聚合指标与告警。两者通过 trace_id 关联。"),
            ("关联", "成本与容量的关系见 arch-capacity；指标命名见 obs-metric-naming。"),
        ],
        related=["obs-trace-spec", "obs-metric-naming", "svc-ops-agent", "arch-capacity"],
    ),
]


# =====================================================================
# 六、命令手册（8 篇）
# =====================================================================

COMMANDS: list[dict] = [
    dict(
        key="cmd-kubectl",
        title="kubectl 排查命令速查",
        category="cmd",
        tags=["k8s", "命令"],
        summary="Pod 异常排查的标准命令序列：先看状态、再看事件、最后看日志与资源。",
        sections=[
            ("状态与事件", "```bash\nkubectl get pods -n trade -o wide\n"
                        "kubectl describe pod <pod> -n trade | grep -A5 -E 'State|Events'\n"
                        "kubectl get events -n trade --sort-by='.lastTimestamp' | tail -20\n```"),
            ("资源占用", "```bash\nkubectl top pod -n trade --containers | sort -k3 -nr | head\nkubectl top node\n```"),
            ("日志", "```bash\nkubectl logs <pod> -n trade --tail=200 -f\n"
                   "kubectl logs <pod> -n trade --previous   # 看崩溃前一个容器的日志\n```"),
            ("进入容器", "```bash\nkubectl exec -it <pod> -n trade -- /bin/sh\n```"),
            ("扩缩容与重启", "```bash\nkubectl scale deploy order-service -n trade --replicas=12\n"
                         "kubectl rollout restart deploy order-service -n trade\nkubectl rollout undo deploy order-service -n trade\n```"),
            ("常见判断", "RESTARTS 增长 + Last State=OOMKilled → rb-pod-oomkilled；Pending → 资源不足或调度失败；"
                      "CrashLoopBackOff → 启动即失败，看 `--previous` 日志。"),
        ],
        related=["rb-pod-oomkilled", "rb-node-notready", "rb-cpu-high"],
    ),
    dict(
        key="cmd-redis-cli",
        title="redis-cli 排查命令速查",
        category="cmd",
        tags=["redis", "命令"],
        summary="Redis Cluster 场景必须加 `-c`，排查顺序：集群健康 → 内存 → 大 key → 慢日志。",
        sections=[
            ("集群健康", "```bash\nredis-cli -c --cluster check 10.0.0.1:6379\nredis-cli -c --cluster nodes | grep -E 'fail|handshake'\n```"),
            ("内存与水位", "```bash\nredis-cli -c --cluster info memory\nredis-cli -c --cluster info keyspace\n```"),
            ("大 key 与热 key", "```bash\nredis-cli -c --cluster bigkeys\nredis-cli --hotkeys -c\n```"),
            ("慢日志", "```bash\nredis-cli -c slowlog get 20\n```"),
            ("在线扩槽", "```bash\nredis-cli --cluster add-node <new>:6379 <existing>:6379\n"
                      "redis-cli --cluster reshard <existing>:6379\n```"),
            ("常见判断", "`OOM command not allowed` → rb-redis-oom；节点反复 fail → rb-redis-failover；"
                      "`maxmemory-policy` 是 noeviction 时必须变更（pol-config-change）。"),
        ],
        related=["rb-redis-oom", "rb-redis-failover", "rb-cache-avalanche", "pol-config-change"],
    ),
    dict(
        key="cmd-mysql",
        title="MySQL 排查命令速查",
        category="cmd",
        tags=["mysql", "命令"],
        summary="两条主线：连接维（谁在连、连了多少）与查询维（谁在跑、跑了多久）。",
        sections=[
            ("连接维", "```sql\nSHOW STATUS LIKE 'Threads_connected';\nSHOW STATUS LIKE 'Threads_running';\nSHOW VARIABLES LIKE 'max_connections';\nSHOW PROCESSLIST;\n```"),
            ("查询维", "```sql\nSHOW FULL PROCESSLIST;      -- 关注 Time 大且 State=executing\nEXPLAIN SELECT ...;         -- 确认是否走索引\n```"),
            ("慢查询分析", "```bash\npt-query-digest /var/log/mysql/slow.log --limit 5\n```"),
            ("锁与死锁", "```sql\nSHOW ENGINE INNODB STATUS\\G   -- 看 LATEST DETECTED DEADLOCK\nSELECT * FROM performance_schema.data_lock_waits;\n```"),
            ("主从延迟", "```sql\nSHOW SLAVE STATUS\\G           -- 看 Seconds_Behind_Master\n```"),
            ("常见判断", "Threads_connected 接近 max_connections → rb-mysql-conn-pool；"
                      "大量 executing → rb-mysql-slow-query；出现 Deadlock → rb-mysql-deadlock。"),
        ],
        related=["rb-mysql-conn-pool", "rb-mysql-slow-query", "rb-mysql-deadlock", "pol-db-change"],
    ),
    dict(
        key="cmd-arthas",
        title="Arthas 排查命令速查（Java 服务）",
        category="cmd",
        tags=["java", "arthas"],
        summary="Java 服务在线诊断首选 Arthas，重点：dashboard（全局）、thread（CPU）、heapdump（内存）、trace（耗时）。",
        sections=[
            ("安装与附加", "```bash\ncurl -O https://arthas.aliyun.com/arthas-boot.jar\njava -jar arthas-boot.jar    # 选择目标进程\n```"),
            ("全局状态", "```bash\ndashboard -n 1      # 线程 / 内存 / GC 一屏总览\n```"),
            ("CPU 定位", "```bash\nthread -n 5         # CPU 占用最高的 5 个线程及堆栈\nthread -b           # 找出阻塞其他线程的线程（死锁）\n```"),
            ("耗时定位", "```bash\ntrace com.example.OrderService createOrder '#cost > 100'\nwatch com.example.OrderService createOrder '{params, returnObj}' -x 2\n```"),
            ("内存", "```bash\nheapdump /tmp/heap.hprof\nsc -d com.example.OrderService    # 查看类加载详情\n```"),
            ("注意", "inventory-service 仍是 Java 11，Arthas 需用 3.7.x 版本；Java 17 服务用最新版即可。"
                   "生产环境慎用 `trace` 全量方法（会显著增加耗时）。"),
        ],
        related=["rb-cpu-high", "rb-pod-oomkilled", "rb-threadpool-exhaust", "svc-inventory-service"],
    ),
    dict(
        key="cmd-tcpdump",
        title="tcpdump 抓包排查",
        category="cmd",
        tags=["网络", "命令"],
        summary="用于定位网络层丢包、重传、连接重置，是「服务都不慢但请求就是失败」的终极手段。",
        sections=[
            ("基础抓包", "```bash\ntcpdump -i eth0 -nn -s0 -w /tmp/cap.pcap host 10.0.0.5 and port 8082\n```"),
            ("实时观察", "```bash\ntcpdump -i eth0 -nn 'tcp[tcpflags] & (tcp-syn|tcp-rst) != 0'\n```"),
            ("丢包与重传", "```bash\nss -s                     # 全局统计（看 retrans）\nnetstat -s | grep -i retrans\n```"),
            ("DNS 相关", "```bash\ntcpdump -i eth0 -nn port 53\n```"),
            ("注意", "容器环境需在宿主机抓包（veth 对应关系用 `ip link` 查看）；生产抓包限定 `-c 1000` 避免写满磁盘（rb-disk-full）。"),
        ],
        related=["rb-dns-failure", "rb-gateway-5xx", "rb-disk-full"],
    ),
    dict(
        key="cmd-curl",
        title="curl 探活与耗时分解",
        category="cmd",
        tags=["命令", "探活"],
        summary="区分「网关慢」还是「上游慢」的最快手段：直连上游并分解各阶段耗时。",
        sections=[
            ("耗时分解", "```bash\ncurl -o /dev/null -s -w 'dns:%{time_namelookup} conn:%{time_connect} "
                      "tls:%{time_appconnect} ttfb:%{time_starttransfer} total:%{time_total} code:%{http_code}\\n' \\\n"
                      "  https://gw.example.com/api/order/health\n```"),
            ("直连上游对比", "```bash\ncurl -s -w '%{http_code} %{time_total}\\n' http://order-service.trade.svc:8082/actuator/health\n```"),
            ("首个 SSE 事件计时（AI 服务 TTFT）", "```bash\ncurl -N -s -w 'ttfb:%{time_starttransfer}\\n' -X POST \\\n"
                                        "  http://localhost:8090/api/chat -H 'Content-Type: application/json' \\\n"
                                        "  -d '{\"query\":\"订单服务 OOM 怎么处理\"}' | head -c 200\n```"),
            ("判断", "ttfb 大而 conn 小 → 服务端处理慢；conn 大 → 网络或握手问题，走 rb-dns-failure / cmd-tcpdump。"),
        ],
        related=["rb-gateway-5xx", "rb-dns-failure", "cmd-tcpdump"],
    ),
    dict(
        key="cmd-jstack",
        title="jstack 线程栈分析",
        category="cmd",
        tags=["java", "命令"],
        summary="jstack 用于分析线程阻塞与死锁，配合 top -Hp 定位高 CPU 线程。",
        sections=[
            ("获取线程栈", "```bash\njstack -l <pid> > /tmp/thread.txt\n# 连续抓 3 次，间隔 5s，对比哪些线程原地不动（典型卡死特征）\njstack <pid> > t1.txt; sleep 5; jstack <pid> > t2.txt; sleep 5; jstack <pid> > t3.txt\n```"),
            ("高 CPU 线程定位", "```bash\ntop -Hp <pid>              # 找出高 CPU 的线程 id（十进制）\nprintf '%x\\n' <tid>        # 转成十六进制\njstack <pid> | grep -A30 <hex>\n```"),
            ("死锁检测", "```bash\njstack -l <pid> | grep -A20 'Found one Java-level deadlock'\n```"),
            ("常见状态", "RUNNABLE（自旋 / 死循环）、BLOCKED（等锁）、WAITING（等条件）、TIMED_WAITING（sleep / 超时等待）。"),
            ("与 arthas 分工", "jstack 是无侵入快照，适合保留证据；arthas 的 `thread -n` 更直观，见 cmd-arthas。"),
        ],
        related=["rb-cpu-high", "rb-threadpool-exhaust", "cmd-arthas"],
    ),
    dict(
        key="cmd-promql",
        title="PromQL 常用查询",
        category="cmd",
        tags=["prometheus", "命令"],
        summary="排查时最常用的 PromQL：错误率、P95/P99、饱和度、依赖耗时。",
        sections=[
            ("错误率", "```promql\nsum(rate(http_requests_total{status=~\"5..\"}[5m])) by (service)\n  / sum(rate(http_requests_total[5m])) by (service)\n```"),
            ("P95 / P99 耗时", "```promql\nhistogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))\n```"),
            ("线程池饱和度", "```promql\nexecutor_active / executor_max   # 接近 1 即排队\n```"),
            ("依赖调用耗时", "```promql\nhistogram_quantile(0.99, sum(rate(client_call_duration_seconds_bucket[5m])) by (le, target))\n```"),
            ("缓存命中率", "```promql\nsum(rate(cache_hits_total[5m])) / sum(rate(cache_requests_total[5m]))\n```"),
            ("Kafka 积压", "```promql\nkafka_consumergroup_lag{group=~\"$group\"}\n```"),
        ],
        related=["obs-metric-naming", "alert-p0-cache-hit", "alert-p1-threadpool"],
    ),
]


# =====================================================================
# 七、FAQ（8 篇）
# =====================================================================

FAQ: list[dict] = [
    dict(
        key="faq-01",
        title="如何查看某个服务的日志",
        category="faq",
        tags=["FAQ", "日志"],
        summary="按 trace_id 或服务名在 Kibana 查询，生产日志保留 14 天。",
        sections=[
            ("入口", "Kibana（日志）或 Grafana 面板下钻；采集链路见 obs-log-pipeline。"),
            ("按服务查", "索引 `app-log-*`，筛选 `service:order-service` 与时间范围。"),
            ("按请求查", "用 `trace_id` 精确筛选，可串起多个服务的日志。"),
            ("容器内看日志", "`kubectl logs <pod> -n trade --tail=200`，崩溃时加 `--previous`。"),
        ],
        related=["obs-log-pipeline", "obs-log-spec", "cmd-kubectl"],
    ),
    dict(
        key="faq-02",
        title="如何申请数据库账号与权限",
        category="faq",
        tags=["FAQ", "权限"],
        summary="走 ticket_sys 提单，遵循最小权限原则，默认只给只读账号。",
        sections=[
            ("流程", "在 ticket_sys 提交「数据库权限申请」工单，注明库名、用途、期限、是否需要写权限。"),
            ("默认策略", "默认授予只读权限；写权限需要直属主管 + DBA 双审批。"),
            ("生产库禁止", "禁止申请生产库 DDL 权限（DDL 必须走 DBA 变更流程，见 pol-db-change）。"),
            ("账号规范", "账号按「服务名 - 环境」命名，如 `order_service_prod`；禁止多人共用账号。"),
            ("时效", "临时权限默认 7 天自动回收。"),
        ],
        related=["pol-db-change", "faq-06"],
    ),
    dict(
        key="faq-03",
        title="如何发起紧急发布",
        category="faq",
        tags=["FAQ", "发布"],
        summary="P0/P1 故障处置需要绕过灰度时走紧急变更流程，事后 24 小时内补单。",
        sections=[
            ("适用", "仅在故障处置场景使用；日常需求必须走标准灰度发布（pol-gray-release）。"),
            ("操作", "在企微 P0 群 @值班负责人 说明「现象 + 要发布的内容 + 预期效果」，获授权后执行。"),
            ("禁止", "禁止在未授权情况下跳过灰度直接全量；禁止发布未经代码评审的变更。"),
            ("事后", "24 小时内补齐变更单，并在复盘会议中说明原因（pol-emergency）。"),
        ],
        related=["pol-emergency", "pol-gray-release", "pol-rollback"],
    ),
    dict(
        key="faq-04",
        title="如何接入监控与告警",
        category="faq",
        tags=["FAQ", "监控"],
        summary="新服务上线必须暴露 /metrics 并配置四条基础告警，缺一不予上线。",
        sections=[
            ("必备", "暴露 Prometheus 格式 `/metrics`；提供 `/healthz` 健康检查；上报链路追踪。"),
            ("基础告警", "服务不可用（实例掉线）、错误率、P95 延迟、资源饱和度，四条缺一不可。"),
            ("指标命名", "遵循 obs-metric-naming，禁止用高基数标签。"),
            ("大盘", "在 Grafana 创建服务大盘（四个黄金信号面板），模板复用现有 dashboard。"),
            ("验收", "上线前用 Grafana 确认指标已采集，并人为触发一次告警验证通知链路。"),
        ],
        related=["obs-metric-naming", "obs-trace-spec", "obs-langfuse"],
    ),
    dict(
        key="faq-05",
        title="如何排查 502 与 504",
        category="faq",
        tags=["FAQ", "排查"],
        summary="502 是上游连不上，504 是上游太慢；先看 nginx error.log 定位具体上游。",
        sections=[
            ("区别", "502 Bad Gateway = 无法连接上游或上游返回非法响应；504 Gateway Timeout = 连上了但超过读超时（3s）。"),
            ("第一步", "`tail -f /var/log/nginx/error.log` 看 `connect() failed`（502）还是 `upstream timed out`（504）。"),
            ("502 排查", "检查上游 Pod 是否就绪、Nacos 实例列表是否含异常实例，见 rb-gateway-5xx。"),
            ("504 排查", "查看上游 P99 耗时；大概率是慢 SQL（rb-mysql-slow-query）或线程池排队（rb-threadpool-exhaust）。"),
            ("快速区分", "直连上游 `curl` 探活（cmd-curl）：直连正常但经网关失败 → 网关或服务发现问题。"),
        ],
        related=["rb-gateway-5xx", "rb-mysql-slow-query", "rb-threadpool-exhaust", "cmd-curl"],
    ),
    dict(
        key="faq-06",
        title="如何申请生产环境权限",
        category="faq",
        tags=["FAQ", "权限"],
        summary="生产权限通过堡垒机申请，遵循最小权限与时效原则，全程审计。",
        sections=[
            ("流程", "ticket_sys 提交「生产权限申请」→ 直属主管审批 → 安全组复核 → 开通堡垒机账号。"),
            ("最小权限", "只开通所需主机的对应命令权限，默认不含 root。"),
            ("审计", "堡垒机全程录屏与命令审计，禁止绕过堡垒机直连生产。"),
            ("时效", "默认 30 天，到期自动回收；长期权限需季度复审。"),
            ("紧急场景", "P0 故障期间可走口头授权临时开通，事后补单（pol-emergency）。"),
        ],
        related=["pol-emergency", "pol-security-incident", "faq-02"],
    ),
    dict(
        key="faq-07",
        title="如何通过智能助手查询工单与资产",
        category="faq",
        tags=["FAQ", "ops-agent", "MCP"],
        summary="运维智能助手 ops-agent 通过 MCP 协议接入工单系统与 CMDB，可用自然语言查询。",
        sections=[
            ("能力", "查询工单状态与历史处理记录、查询资产归属与负责人、查询服务部署信息。"),
            ("接入方式", "通过 MCP（Model Context Protocol）接入，MCP Server 用 Java 实现，对接 ticket_sys 与 CMDB。"),
            ("示例提问", "「帮我查一下 INC-2026-1024 这个工单的处理进度」「order-service 的负责人是谁」「哪些服务部署在 trade 命名空间」。"),
            ("降级行为", "MCP 工具不可用时，助手会标记该工具不可用并走无工具链路，不会直接报错（错误码 AGT-500-001）。"),
            ("数据边界", "只读查询，助手不具备修改工单或资产的权限。"),
        ],
        related=["svc-ops-agent", "err-ops-agent"],
    ),
    dict(
        key="faq-08",
        title="如何加入值班轮换",
        category="faq",
        tags=["FAQ", "值班"],
        summary="新成员需完成三关：Runbook 考核、跟班观察、独立值守。",
        sections=[
            ("准入要求", "① 完成 Runbook 学习并通过考核（≥ 80 分）② 完成 2 次跟班观察 ③ 完成 1 次演练（oncall-drill）。"),
            ("轮换周期", "一周一班次，交接见 oncall-shift。"),
            ("能力要求", "能独立处置 P1 及以下；P0 需在有经验者陪同下处置。"),
            ("支持", "疑难问题可在值班群求助，或使用 ops-agent 查询处置步骤。"),
        ],
        related=["oncall-duty", "oncall-shift", "oncall-drill"],
    ),
]


# 汇总出口，供渲染器与校验器统一使用
# 注意：ALERTS / POLICIES / ARCH 定义在同级的 policies_arch.py 中。
ALL_EXTRA_GROUPS = {
    "oncall": ONCALL,
    "obs": OBSERVABILITY,
    "cmd": COMMANDS,
    "faq": FAQ,
}
