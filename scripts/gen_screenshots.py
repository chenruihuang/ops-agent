"""生成 20 张运维"截图"素材（HTML -> Chrome headless -> PNG）。

设计要点：
  1. 内容与文本语料**呼应**：同一故障既有 Markdown 文档又有截图，
     这正是多模态知识库（文本 + 图片双模态）的演示素材。
  2. 文字 100% 精确可 OCR —— 浏览器真实渲染，不是 AI 生图（AI 生图文字必然乱码，
     OCR 环节就没有评估意义了）。
  3. 两类视觉形态：终端日志（黑底等宽）与监控面板（Grafana 深色），
     更接近真实运维截图的多样性。
  4. 2x 缩放输出（2480x1380），保证 OCR 输入分辨率充足。

用法：
    python -m scripts.gen_screenshots            # 全部生成（结束后自动清理临时文件）
    python -m scripts.gen_screenshots --only img-java-oom-heap
    python -m scripts.gen_screenshots --keep-tmp # 保留 HTML 与 Chrome profile 以便排查
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMG_DIR = ROOT / "data" / "raw" / "images"
TMP_DIR = IMG_DIR / "_tmp"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

SCALE = 2
W, H = 1240, 690

TODAY = date.today().isoformat()

# =====================================================================
# 20 个截图场景
# 每行：(样式类, 文本)；样式类 ts/err/warn/ok/info/cmd/dim/hl
# =====================================================================

SCENES: list[dict] = [
    dict(
        id="img-java-oom-heap",
        title="order-service - kubectl logs -f (容器 3)",
        caption="订单服务容器 OOM 堆栈 —— 与 rb-pod-oomkilled 对应",
        related=["rb-pod-oomkilled", "svc-order-service", "alert-p1-pod-restart"],
        lines=[
            ("ts", "2026-10-16 09:41:02.118 INFO  [order-exec-17] OrderService - 订单创建成功 orderNo=SO20261016094102118 sku=88213"),
            ("warn", "2026-10-16 09:41:20.455 WARN  [GC-alarm] GC overhead - Old Gen used 3812MB/4096MB (93.1%), Full GC count last 5m = 7"),
            ("err", "2026-10-16 09:41:23.190 ERROR [http-nio-8082-exec-204] o.a.c.c.C.[.[.[.[dispatcherServlet] - Servlet.service() threw exception"),
            ("err", "java.lang.OutOfMemoryError: Java heap space"),
            ("plain", "\tat java.base/java.util.Arrays.copyOf(Arrays.java:3512)"),
            ("plain", "\tat com.example.order.cache.LocalSkuCache.putAll(LocalSkuCache.java:87)"),
            ("plain", "\tat com.example.order.cache.LocalSkuCache$SyncTask.run(LocalSkuCache.java:141)"),
            ("err", "2026-10-16 09:41:24.002 ERROR - JVM heap dump triggered: -XX:+HeapDumpOnOutOfMemoryError -> /data/logs/heap_094124.hprof"),
            ("dim", "2026-10-16 09:41:24.301 INFO  - Container will be terminated by Kubernetes (exit 137 expected)"),
            ("info", "2026-10-16 09:41:24.302 INFO  - Pod order-service-7d9f8b6c4-x2k9n restartCount=4 (limit 5)"),
            ("cmd", "$ kubectl top pod order-service-7d9f8b6c4-x2k9n -n trade --containers"),
            ("ok",  "POD                             CONTAINER  CPU(cores)  MEMORY(bytes)"),
            ("ok",  "order-service-7d9f8b6c4-x2k9n   order      1421m       5127Mi (limit 5Gi)"),
            ("hl",  "# 判读：内存贴 limit + OOMKilled -> 处置见 rb-pod-oomkilled；历史泄漏先看 heap dump"),
        ],
    ),
    dict(
        id="img-k8s-oomkilled",
        title="kubectl describe pod order-service-7d9f8b6c4-x2k9n -n trade",
        caption="K8s Pod OOMKilled 详情（Exit Code 137）—— 与 rb-pod-oomkilled 对应",
        related=["rb-pod-oomkilled", "cmd-kubectl", "alert-p1-pod-restart"],
        lines=[
            ("cmd", "$ kubectl describe pod order-service-7d9f8b6c4-x2k9n -n trade"),
            ("dim", "Name:         order-service-7d9f8b6c4-x2k9n"),
            ("dim", "Namespace:    trade"),
            ("warn", "Last State:   Terminated"),
            ("hl",  "  Reason:     OOMKilled"),
            ("hl",  "  Exit Code:  137"),
            ("dim", "  Started:    2026-10-16T09:21:47+08:00"),
            ("dim", "  Finished:   2026-10-16T09:41:24+08:00"),
            ("warn", "Restart Count: 4 (limit: 5)"),
            ("dim", "Limits:"),
            ("dim", "  cpu:     2"),
            ("hl",  "  memory:  5Gi"),
            ("dim", "Requests:"),
            ("dim", "  cpu:     1"),
            ("dim", "  memory:  4Gi"),
            ("info", "Events:"),
            ("warn", "  Warning  BackOff  3m12s  Back-off restarting failed container"),
            ("ok",  "  Normal   Pulled   2m40s  Container image \"registry.corp/trade/order-service:2026.10.12-3\" pulled"),
            ("hl",  "# 判读：Reason=OOMKilled + restartCount 增长 -> rb-pod-oomkilled；-Xmx4g 应设为 limit 的 75% 以内"),
        ],
    ),
    dict(
        id="img-mysql-too-many-connections",
        title="mysql - order_db (master) - 10.0.2.11",
        caption="MySQL 连接数逼近上限 —— 与 rb-mysql-conn-pool 对应",
        related=["rb-mysql-conn-pool", "rb-mysql-slow-query", "alert-p1-db-conn", "err-order-service"],
        lines=[
            ("cmd", "$ mysql -h order-db-master -e \"SHOW STATUS LIKE 'Threads_connected'; SHOW VARIABLES LIKE 'max_connections';\""),
            ("ok",  "Variable_name      Value"),
            ("hl",  "Threads_connected  1741"),
            ("hl",  "max_connections    2000"),
            ("warn", "# Threads_connected / max_connections = 87.05%  (> 85% 告警阈值 alert-p1-db-conn)"),
            ("cmd", "$ mysql -e \"SHOW FULL PROCESSLIST\" | awk '{print $5, $6, $7}' | sort | uniq -c | sort -rn | head -5"),
            ("ok",  "   3  Query      executing"),
            ("warn", " 812  Sleep      (idle > 300s, 应用侧疑似连接泄漏)"),
            ("warn", " 644  Query      Sending data"),
            ("err", "2026-10-16 09:38:11 [HikariPool-1 housekeeper] WARN - (order-service) - Thread starvation or clock leap detected"),
            ("err", "com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException: Lock wait timeout exceeded; try starting transaction"),
            ("err", "HikariPool-1 - Connection is not available, request timed out after 30000ms"),
            ("hl",  "# 判读：Sleep 大量堆积 = 连接泄漏；Sending data 多 = 慢 SQL -> 先 rb-mysql-conn-pool 止血再 rb-mysql-slow-query"),
        ],
    ),
    dict(
        id="img-mysql-slow-log",
        title="/var/log/mysql/slow.log - order_db",
        caption="MySQL 慢查询日志 —— 与 rb-mysql-slow-query 对应",
        related=["rb-mysql-slow-query", "cmd-mysql", "alert-p1-slow-sql", "pol-db-change"],
        lines=[
            ("cmd", "$ pt-query-digest /var/log/mysql/slow.log --limit 3"),
            ("dim", "# 210ms user time, 1.2s system time, max 4.21s, Rank Query ID response time"),
            ("warn", "# Rank 1: QPS 3.10x, 38.2% (4.21s), SELECT order_db.order_main"),
            ("hl",  "SELECT * FROM order_main WHERE user_phone = '13800138000' AND status IN ('PAID','SHIPPED') ORDER BY created_at DESC LIMIT 50;"),
            ("dim", "  -- Query_time: 4.214  Lock_time: 0.000  Rows_sent: 50  Rows_examined: 4,211,382"),
            ("err", "  -- Rows_examined/Rows_sent: 84,227.6  (全表扫描，未走索引)"),
            ("warn", "# Rank 2: QPS 1.42x, 12.6% (1.39s), UPDATE inventory_db.stock"),
            ("hl",  "UPDATE stock SET available = available - 1 WHERE sku_id = 88213 AND available > 0;"),
            ("dim", "  -- Query_time: 1.392  Lock_time: 0.844  Rows_examined: 1"),
            ("warn", "# Rank 3: QPS 0.88x, 9.1% (1.01s), SELECT order_db.order_item"),
            ("dim", "  -- Query_time: 1.009  Lock_time: 0.000  Rows_examined: 302,114"),
            ("hl",  "# 判读：Rank1 缺 user_phone 索引（走 pol-db-change 补索引）；Rank2 锁等待长 -> 并发热点"),
        ],
    ),
    dict(
        id="img-mysql-deadlock",
        title="mysql inventory_db - SHOW ENGINE INNODB STATUS (节选)",
        caption="InnoDB 死锁日志 —— 与 rb-mysql-deadlock 对应",
        related=["rb-mysql-deadlock", "err-inventory-service", "svc-inventory-service"],
        lines=[
            ("cmd", "$ mysql -e \"SHOW ENGINE INNODB STATUS\\G\" | sed -n '/LATEST DETECTED DEADLOCK/,/^---/p'"),
            ("warn", "------------------------"),
            ("hl",  "LATEST DETECTED DEADLOCK"),
            ("warn", "------------------------"),
            ("dim", "2026-10-16 09:52:07 0x7f8a2c3d4700"),
            ("plain", "*** (1) TRANSACTION:"),
            ("plain", "TRANSACTION 41288476, ACTIVE 0 sec inserting"),
            ("hl",  "mysql tables in use 1, locked 1"),
            ("plain", "INSERT INTO stock_deduct(sku_id, order_no, qty) VALUES (88213,'SO202610160952',2)"),
            ("plain", "*** (2) TRANSACTION:"),
            ("plain", "TRANSACTION 41288477, ACTIVE 0 sec updating"),
            ("hl",  "UPDATE stock SET available = available - 2, locked = locked + 2 WHERE sku_id = 88213;"),
            ("err", "*** WE ROLL BACK TRANSACTION (2)"),
            ("warn", "死锁计数（最近 1h）：47 次  |  影响错误码：INV-500-002"),
            ("hl",  "# 判读：批量扣减顺序不一致导致死锁 -> 统一按 sku_id 升序更新，见 rb-mysql-deadlock"),
        ],
    ),
    dict(
        id="img-redis-oom",
        title="redis-cli -c user-cache (10.0.3.21:6379)",
        caption="Redis OOM 拒绝写入 —— 与 rb-redis-oom 对应",
        related=["rb-redis-oom", "err-user-center", "alert-p1-redis-mem", "pol-config-change"],
        lines=[
            ("cmd", "$ redis-cli -c -h 10.0.3.21 SET session:u_718230 '{\"v\":1}' PX 1800000"),
            ("err", "(error) OOM command not allowed when used memory > 'maxmemory'."),
            ("cmd", "$ redis-cli -c -h 10.0.3.21 info memory | grep -E 'used_memory_human|maxmemory_human|maxmemory_policy'"),
            ("ok",  "used_memory_human:7.68G"),
            ("hl",  "maxmemory_human:8.00G   (96.0%)"),
            ("err", "maxmemory_policy:noeviction"),
            ("cmd", "$ redis-cli -c -h 10.0.3.21 --cluster info keyspace | head -4"),
            ("ok",  "db0:keys=41,238,190,expires=39,101,552"),
            ("hl",  "# 无 TTL key 数量: 2,136,638  (5.2%, 全部来自历史版本 report_job 中间结果)"),
            ("cmd", "$ redis-cli -c -h 10.0.3.21 --cluster bigkeys"),
            ("warn", "[WARNING] Biggest string found at 'cache:snapshot:activity_1024' (128.7 MB)"),
            ("hl",  "# 判读：noeviction + 无 TTL key 累积 -> rb-redis-oom；policy 变更走 pol-config-change"),
        ],
    ),
    dict(
        id="img-redis-failover",
        title="redis cluster events - ops-monitor",
        caption="Redis 主从切换连接风暴 —— 与 rb-redis-failover 对应",
        related=["rb-redis-failover", "rb-cache-avalanche", "rb-node-notready"],
        lines=[
            ("warn", "10:02:11.402 [WARN] node 10.0.3.21:6379 (master, slots 0-5460) marked as PFAIL by 3/6 masters"),
            ("err", "10:02:12.055 [FAIL] node 10.0.3.21:6379 -> FAIL, triggering failover"),
            ("info", "10:02:12.318 [INFO] replica 10.0.3.22:6379 elected as new master for slots 0-5460"),
            ("warn", "10:02:12.402 [WARN] client reconnect storm: 3,842 connections in 1.2s (baseline 412)"),
            ("err", "10:02:13.118 [ERROR] new master CPU 98%, slowlog +214 entries/s"),
            ("err", "10:02:14.220 [FAIL] node 10.0.3.22:6379 PFAIL again (flapping)"),
            ("dim", "cluster-node-timeout: 5000ms   # 过小，易误判"),
            ("ok",  "10:02:15.001 [ACTION] 调整 cluster-node-timeout -> 15000ms, 客户端连接池 maxTotal 200->80, 重连退避 50ms->500ms"),
            ("ok",  "10:02:18.774 [RECOVER] cluster state: ok, 3 masters + 3 replicas stable"),
            ("hl",  "# 判读：failover 本身正常，异常在连接风暴；限流客户端重连是第一动作，见 rb-redis-failover"),
        ],
    ),
    dict(
        id="img-nginx-502",
        title="/var/log/nginx/error.log - gateway-0",
        caption="网关 502：上游连接失败 —— 与 rb-gateway-5xx 对应",
        related=["rb-gateway-5xx", "err-gateway", "rb-gateway-conn-exhaust", "alert-p0-gateway-5xx"],
        lines=[
            ("cmd", "$ tail -f /var/log/nginx/error.log"),
            ("err", "2026/10/16 10:11:02 [error] 4712#0: *8841231 connect() failed (111: Connection refused) while connecting to upstream,"),
            ("dim", "  client: 10.24.16.7, server: gw.example.com, request: \"POST /api/order/create HTTP/1.1\","),
            ("hl",  "  upstream: \"http://10.0.1.18:8082/api/order/create\", host: \"gw.example.com\""),
            ("err", "2026/10/16 10:11:02 [error] 4712#0: *8841244 no live upstreams while connecting to upstream"),
            ("dim", "  upstream: \"order_service_pool\", request: \"POST /api/order/create\""),
            ("cmd", "$ awk '{print $9}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -5"),
            ("ok",  "  41812 200"),
            ("err", "   3941 502   (2.1% -> 已超过 alert-p0-gateway-5xx 的 0.5% 阈值)"),
            ("warn", "    187 499"),
            ("cmd", "$ curl -s -o /dev/null -w '%{http_code} %{time_total}s\\n' http://10.0.1.18:8082/actuator/health"),
            ("err", "000 0.002s   (直连也失败 -> 上游实例挂了，非网关问题)"),
            ("hl",  "# 判读：单一上游 Connection refused -> 检查 order-service Pod 状态与 Nacos 实例列表，见 rb-gateway-5xx"),
        ],
    ),
    dict(
        id="img-nginx-504",
        title="/var/log/nginx/error.log - gateway-2",
        caption="网关 504：上游读超时 —— 与 rb-gateway-5xx / rb-threadpool-exhaust 对应",
        related=["rb-gateway-5xx", "rb-threadpool-exhaust", "rb-full-link-timeout", "cmd-curl"],
        lines=[
            ("cmd", "$ grep 'upstream timed out' /var/log/nginx/error.log | tail -3"),
            ("err", "2026/10/16 10:20:31 [error] 5211#0: *9120842 upstream timed out (110: Connection timed out) while reading response header from upstream,"),
            ("dim", "  client: 10.24.31.9, request: \"GET /api/order/list?page=1&size=50 HTTP/1.1\","),
            ("hl",  "  upstream: \"http://10.0.1.22:8082/api/order/list\", read timeout 3s"),
            ("cmd", "$ curl -o /dev/null -s -w 'dns:%{time_namelookup} conn:%{time_connect} ttfb:%{time_starttransfer} total:%{time_total} code:%{http_code}\\n' \\"),            ("ok",  "  http://10.0.1.22:8082/api/order/list?page=1&size=50"),
            ("hl",  "dns:0.001 conn:0.003 ttfb:6.842 total:6.842 code:200   # conn 快但 ttfb 慢 -> 服务端处理慢"),
            ("dim", "# 同接口直连 P99: 6.9s，经网关 3s 超时截断 -> 504"),
            ("cmd", "$ curl -s http://10.0.1.22:9082/actuator/metrics/executor.queued | jq"),
            ("warn", "{\"measurements\":[{\"statistic\":\"VALUE\",\"value\":1000.0}]}   # 队列 1000/1000 已满"),
            ("hl",  "# 判读：慢在服务端（线程池排队）而非网络 -> rb-threadpool-exhaust + rb-full-link-timeout"),
        ],
    ),
    dict(
        id="img-kafka-lag",
        title="Kafka 消费组监控 - Grafana 面板",
        caption="Kafka 消费积压 —— 与 rb-mq-backlog / alert-p1-mq-lag 对应",
        related=["rb-mq-backlog", "alert-p1-mq-lag", "svc-search-service", "svc-notify-service"],
        kind="panel",
        metrics=[
            ("product-change-indexer", "245,183", "#ff5f56", "分区 12 / 消费者 4"),
            ("notify-dispatcher", "18,204", "#f5c542", "分区 6 / 消费者 6"),
            ("pay-callback-consumer", "0", "#4ade80", "分区 3 / 消费者 3"),
        ],
        table=[
            ("TOPIC", "GROUP", "PARTITION", "CURRENT-OFFSET", "LOG-END-OFFSET", "LAG", "状态"),
            ("product-change-topic", "search-indexer", "3", "882,133", "902,451", "14,318", "消费中"),
            ("product-change-topic", "search-indexer", "7", "411,902", "641,902", "230,000", "阻塞"),
            ("notify-topic", "notify-dispatcher", "2", "1,224,553", "1,242,757", "18,204", "重试中"),
        ],
        annotation="消费者数(4) < 分区数(12)，并行度不足；分区 7 存在异常消息阻塞 -> 处置见 rb-mq-backlog",
    ),
    dict(
        id="img-es-red",
        title="elasticsearch - _cluster/health - es-prod",
        caption="ES 集群 red 状态 —— 与 rb-es-cluster-red 对应",
        related=["rb-es-cluster-red", "err-search-service", "alert-p0-es-red", "arch-es-reindex"],
        lines=[
            ("cmd", "$ curl -s localhost:9200/_cluster/health?pretty"),
            ("ok",  "{"),
            ("dim", "  \"cluster_name\" : \"es-prod\","),
            ("hl",  "  \"status\" : \"red\","),
            ("dim", "  \"timed_out\" : false,"),
            ("dim", "  \"number_of_nodes\" : 9,"),
            ("dim", "  \"number_of_data_nodes\" : 6,"),
            ("err", "  \"active_primary_shards\" : 214,"),
            ("err", "  \"active_shards\" : 396,"),
            ("warn", "  \"unassigned_shards\" : 16,"),
            ("dim", "  \"delayed_unassigned_shards\" : 0,"),
            ("warn", "  \"number_of_pending_tasks\" : 118"),
            ("ok",  "}"),
            ("cmd", "$ curl -s localhost:9200/_cat/shards?v | grep UNASSIGNED | head -3"),
            ("err", "product_v8 0 r UNASSIGNED"),
            ("err", "product_v8 4 r UNASSIGNED"),
            ("warn", "app-log-2026.10.16 2 p UNASSIGNED"),
            ("hl",  "# 判读：red = 有主分片未分配（app-log-2026.10.16 2 p）-> 第一动作 _cluster/allocation/explain，见 rb-es-cluster-red"),
        ],
    ),
    dict(
        id="img-disk-full",
        title="bash - file-service-node-3 (MinIO 数据盘)",
        caption="磁盘空间耗尽 —— 与 rb-disk-full / FILE-507-001 对应",
        related=["rb-disk-full", "err-file-service", "rb-inode-exhaust", "alert-p1-disk-usage", "svc-file-service"],
        lines=[
            ("cmd", "$ df -h /data"),
            ("ok",  "Filesystem      Size  Used Avail Use% Mounted on"),
            ("err", "/dev/sdb1       2.0T  2.0T   28K 100% /data"),
            ("cmd", "$ du -sh /data/* 2>/dev/null | sort -rh | head -4"),
            ("ok",  "1.4T  /data/minio/upload-prod"),
            ("warn", "412G  /data/logs"),
            ("err", "187G  /data/logs/heap_dump  (历史 heap dump 未清理)"),
            ("ok",  " 31G  /data/tmp"),
            ("err", "ERROR 09:57:41 [FileUploadService] - 上传失败: java.io.IOException: No space left on device"),
            ("err", "ERROR 09:57:41 错误码 FILE-507-001 对象存储空间不足"),
            ("cmd", "$ find /var/log -name '*.gz' -mtime +7 | wc -l"),
            ("ok",  "2,413  (可安全清理，见 rb-disk-full 步骤 ①)"),
            ("hl",  "# 判读：heap dump 目录 187G 是隐性杀手 -> 先删可再生的 dump，MinIO 真实增长走扩容"),
        ],
    ),
    dict(
        id="img-inode-exhaust",
        title="bash - report-worker-1",
        caption="inode 耗尽（df -h 正常但写失败）—— 与 rb-inode-exhaust 对应",
        related=["rb-inode-exhaust", "rb-disk-full", "svc-report-job"],
        lines=[
            ("err", "OSError: [Errno 28] No space left on device: '/data/tmp/celery_task_88412.pkl'"),
            ("cmd", "$ df -h /data"),
            ("ok",  "Filesystem      Size  Used Avail Use% Mounted on"),
            ("ok",  "/dev/sdc1       500G  212G  288G  43% /data        # 空间充足！"),
            ("cmd", "$ df -i /data"),
            ("ok",  "Filesystem      Inodes   IUsed   IFree IUse% Mounted on"),
            ("hl",  "/dev/sdc1       32.0M   32.0M       0  100% /data        # inode 耗尽"),
            ("cmd", "$ for d in /data/*; do echo \"$(find $d -xdev 2>/dev/null | wc -l) $d\"; done | sort -rn | head -3"),
            ("warn", "29,841,203  /data/tmp/report_chunks"),
            ("ok",  "   2,114,004  /data/minio"),
            ("dim", "# 报表任务按秒切分文件，单目录 2984 万个小文件"),
            ("hl",  "# 判读：空间没满但 inode 满 —— 只看 df -h 会误判为「磁盘没满」，见 rb-inode-exhaust"),
        ],
    ),
    dict(
        id="img-threadpool-reject",
        title="order-service - 应用日志 (线程池)",
        caption="线程池拒绝异常 —— 与 rb-threadpool-exhaust / ORD-503-002 对应",
        related=["rb-threadpool-exhaust", "err-order-service", "alert-p1-threadpool", "rb-full-link-timeout"],
        lines=[
            ("err", "2026-10-16 10:31:22.091 ERROR [http-nio-8082-exec-11] c.e.o.s.OrderSubmitService - 订单提交失败"),
            ("hl",  "java.util.concurrent.RejectedExecutionException: Task org.springframework.cloud.sleuth.instrument.async.TraceAsync@7f2a1b rejected from java.util.concurrent.ThreadPoolExecutor@3b81f\""),
            ("dim", "[Running, pool size = 400, active threads = 400, queued tasks = 1000, completed tasks = 884,213]"),
            ("err", "2026-10-16 10:31:22.092 ERROR - 错误码 ORD-503-002 订单处理线程池已满, 1 分钟内第 843 次"),
            ("cmd", "$ curl -s localhost:9082/actuator/threaddump | jq -r '.threads[] | select(.threadName | startswith(\"order-pool\")) | .threadState' | sort | uniq -c"),
            ("ok",  " 318 WAITING   (等待支付网关响应, 平均 2.9s)"),
            ("ok",  "  82 RUNNABLE"),
            ("dim", "# 318/400 线程阻塞在支付渠道调用 -> 下游慢导致线程耗尽"),
            ("hl",  "# 判读：不要调大队列！先看线程卡在哪 —— 短化渠道超时 + 线程池隔离，见 rb-threadpool-exhaust"),
        ],
    ),
    dict(
        id="img-dns-unknownhost",
        title="payment-gateway - 应用日志",
        caption="DNS 解析失败 —— 与 rb-dns-failure 对应",
        related=["rb-dns-failure", "cmd-tcpdump", "rb-gateway-5xx", "rb-node-notready"],
        lines=[
            ("err", "2026-10-16 10:44:12.338 ERROR [pay-channel-2] c.e.p.client.AlipayClient - 渠道调用失败"),
            ("hl",  "java.net.UnknownHostException: openapi.alipay.com: Name or service not known"),
            ("err", "2026-10-16 10:44:12.402 ERROR [pay-channel-5] c.e.p.client.WechatPayClient - api.mch.weixin.qq.com: Temporary failure in name resolution"),
            ("cmd", "$ nslookup openapi.alipay.com"),
            ("warn", ";; connection timed out; no servers could be reached"),
            ("cmd", "$ cat /etc/resolv.conf"),
            ("dim", "nameserver 10.96.0.10"),
            ("dim", "options ndots:5"),
            ("cmd", "$ kubectl -n kube-system get pods -l k8s-app=kube-dns"),
            ("warn", "NAME                      READY   STATUS    RESTARTS      AGE"),
            ("warn", "coredns-5d78c9869d-8k2xq  0/1     Running   4 (2m ago)    118d"),
            ("hl",  "# 判读：CoreDNS Pod 不健康（READY 0/1）且 RESTARTS 增长 -> rb-dns-failure；注意 ndots:5 会放大查询"),
        ],
    ),
    dict(
        id="img-cert-expired",
        title="terminal - 证书巡检",
        caption="TLS 证书过期 —— 与 rb-cert-expired / alert-p2-cert-expire 对应",
        related=["rb-cert-expired", "alert-p2-cert-expire", "svc-gateway", "svc-payment-gateway"],
        lines=[
            ("cmd", "$ echo | openssl s_client -connect gw.example.com:8443 -servername gw.example.com 2>/dev/null | openssl x509 -noout -dates"),
            ("err", "notAfter=Oct 16 08:00:00 2026 GMT    # 已于今日 08:00 过期"),
            ("cmd", "$ curl -v https://gw.example.com/healthz 2>&1 | grep -E 'SSL|error'"),
            ("err", "curl: (60) SSL certificate problem: certificate has expired"),
            ("err", "2026-10-16 08:01:12 [ERROR] pay-channel-1 - 微信支付 v3 平台证书校验失败: certificate has expired or is not yet valid"),
            ("warn", "# 影响：HTTPS 全站不可访问；支付渠道调用全部失败（渠道侧校验我方证书）"),
            ("cmd", "$ kubectl -n infra get certificaterequest | tail -2"),
            ("warn", "gw-tls-4x9p2   False   36h   Failed: ACME challenge failed (DNS-01 timeout)"),
            ("hl",  "# 判读：cert-manager 自动续签失败（ACME DNS-01 超时）-> 手工签发应急 + 修复续签，见 rb-cert-expired"),
        ],
    ),
    dict(
        id="img-arthas-thread",
        title="arthas - inventory-service-5b8d4c (Java 11)",
        caption="Arthas CPU 热点定位 —— 与 rb-cpu-high / cmd-arthas 对应",
        related=["rb-cpu-high", "cmd-arthas", "svc-inventory-service", "cmd-jstack"],
        lines=[
            ("cmd", "$ java -jar arthas-boot.jar  # attach PID 1 (inventory-service)"),
            ("ok",  "Found one java process: 1 [inventory-service-5b8d4c]"),
            ("cmd", "[arthas@1]$ thread -n 3"),
            ("hl",  "\"http-nio-8084-exec-88\" Id=204 cpuUsage=98.31%"),
            ("dim", "    at com.example.inventory.hot.HotSkuDetector.detect(HotSkuDetector.java:64)"),
            ("dim", "    at com.example.inventory.hot.HotSkuDetector$$FastClassBySpring...invoke(<generated>)"),
            ("dim", "    at com.example.inventory.controller.StockController.deduct(StockController.java:45)"),
            ("hl",  "\"http-nio-8084-exec-91\" Id=207 cpuUsage=97.88%"),
            ("dim", "    at com.example.inventory.hot.HotSkuDetector.detect(HotSkuDetector.java:64)"),
            ("cmd", "[arthas@1]$ dashboard -n 1"),
            ("ok",  "GC: young 41次/2.3s  old 3次  |  CPU 1820m/2000m (91%)  |  Threads 412"),
            ("warn", "# HotSkuDetector.detect 第 64 行：正则回溯 + 无缓存的热点判定，在热点 SKU 请求下退化为 O(n^2)"),
            ("hl",  "# 判读：CPU 密集（非 GC 密集）-> 定位到具体代码行，回滚或热修，见 rb-cpu-high"),
        ],
    ),
    dict(
        id="img-gc-fullgc",
        title="GC 日志 - order-service-7d9f8b6c4-pq8xz",
        caption="Full GC 频繁 —— 与 rb-pod-oomkilled（GC 密集型变体）对应",
        related=["rb-pod-oomkilled", "rb-cpu-high", "cmd-arthas"],
        lines=[
            ("cmd", "$ kubectl logs order-service-7d9f8b6c4-pq8xz -n trade | grep '\\[gc\\]' | tail -8"),
            ("dim", "[gc] GC(812) Pause Young (Normal) (G1 Evacuation Pause) 2981M->1912M(4096M) 42.118ms"),
            ("warn", "[gc] GC(813) Pause Full (G1 Compaction Pause) 3712M->3402M(4096M) 1.842s"),
            ("err", "[gc] GC(814) Pause Full (G1 Compaction Pause) 3811M->3788M(4096M) 2.104s"),
            ("err", "[gc] GC(815) Pause Full (G1 Compaction Pause) 3844M->3831M(4096M) 2.317s"),
            ("warn", "[gc] GC(815) Pause Full ...  reclaimed only 13M   # 回收效果极差 = 对象被强持有"),
            ("dim", "[gc] Heap: 3831M used, 4096M total  |  Metaspace: 218M  |  DirectBuffers: 612M"),
            ("warn", "# 观察：Full GC 间隔从 8min 缩短到 40s，单次耗时 2s+，且回收量持续缩小"),
            ("hl",  "# 判读：回收无效说明内存泄漏（非流量问题）-> jmap -histo:live + MAT 分析 dominator tree，见 rb-pod-oomkilled"),
        ],
    ),
    dict(
        id="img-grafana-alert",
        title="Grafana - 值班总览大盘 (P0/P1)",
        caption="监控面板：多条告警同时 firing（雪崩前兆）—— 与 rb-full-link-timeout 对应",
        related=["rb-full-link-timeout", "alert-p0-gateway-5xx", "alert-p0-cache-hit", "alert-p0-link-timeout", "obs-metric-naming"],
        kind="panel",
        metrics=[
            ("网关 5xx 比例", "5.42%", "#ff5f56", "阈值 0.5% · 持续 1m"),
            ("缓存命中率", "31.7%", "#ff5f56", "阈值 90% · 持续 2m"),
            ("全链路 P99", "8.24s", "#ff5f56", "阈值 2s · 持续 2m"),
            ("DB 连接池水位", "87.1%", "#f5c542", "阈值 85%"),
        ],
        table=[
            ("时间", "告警", "级别", "状态", "值班响应"),
            ("10:21:08", "alert-p0-cache-hit 缓存命中率骤降", "P0", "Firing", "@infra-oncall 已认领"),
            ("10:22:31", "alert-p0-gateway-5xx 网关 5xx 比例过高", "P0", "Firing", "@infra-oncall 已认领"),
            ("10:23:40", "alert-p0-link-timeout 全链路 P99 超时", "P0", "Firing", "@trade-oncall 已认领"),
            ("10:24:12", "alert-p1-db-conn MySQL 连接数过高", "P1", "Firing", "未认领"),
        ],
        annotation="多告警级联 = 雪崩特征：缓存先崩（命中率 31.7%）→ DB 连接耗尽 → 网关 5xx → 全链路超时。先断链止血，见 rb-full-link-timeout",
    ),
    dict(
        id="img-pay-callback-timeout",
        title="payment-gateway - 渠道调用与回调日志",
        caption="支付回调超时 —— 与 rb-pay-callback-timeout / PAY-504-101 对应",
        related=["rb-pay-callback-timeout", "err-payment-gateway", "rb-mq-backlog", "alert-p0-pay-callback"],
        lines=[
            ("cmd", "$ kubectl logs payment-gateway-0 -n pay --since=10m | grep -E 'PAY-|callback' | tail -8"),
            ("err", "10:52:03.221 ERROR [channel-wechat-3] c.e.p.c.WechatPayClient - 渠道调用超时 PAY-504-101 url=https://api.mch.weixin.qq.com/v3/pay/transactions/elappid cost=3.002s (timeout=3s)"),
            ("warn", "10:52:03.222 WARN  [channel-wechat-3] CircuitBreaker[pay-wechat] - 失败率 34.7% > 30%，进入 OPEN 状态 60s"),
            ("info", "10:52:03.223 INFO  [channel-wechat-3] 降级为「排队中」异步受理 (PAY-503-102 前置状态)"),
            ("err", "10:52:41.880 ERROR [callback-listener] 回调受理延迟: pay-callback-topic LAG=1,428 (阈值 1000, alert-p0-pay-callback)"),
            ("warn", "10:52:41.881 WARN  订单状态统计: 「支付中」超过 5 分钟的订单 217 笔"),
            ("err", "10:53:02.440 ERROR 幂等冲突检测: PAY-409-103 重复回调 order_no=SO20261016105288, 已按首次结果返回"),
            ("hl",  "# 绝对禁止：重启支付服务（会打断资金流水）-> 先扩消费者 + 对账兜底，见 rb-pay-callback-timeout"),
        ],
    ),
]


# ---------------------------------------------------------------------
# 渲染模板
# ---------------------------------------------------------------------

_CSS_TERMINAL = """
html,body{margin:0;padding:0;background:#05060a;}
.window{width:1240px;height:690px;background:#0b0d12;border:1px solid #23262e;border-radius:8px;
  overflow:hidden;box-sizing:border-box;font-family:Consolas,'Courier New','Microsoft YaHei',monospace;}
.titlebar{height:36px;background:#161a22;display:flex;align-items:center;padding:0 14px;gap:8px;
  border-bottom:1px solid #23262e;}
.dot{width:12px;height:12px;border-radius:50%;display:inline-block;}
.r{background:#ff5f56}.y{background:#f5c542}.g{background:#27c93f}
.title{color:#9aa1ad;font-size:13px;margin-left:12px;font-family:'Microsoft YaHei',sans-serif;}
.body{padding:16px 20px;font-size:15px;line-height:1.62;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;}
.ts{color:#5d6673}
.err{color:#ff5f56;font-weight:bold}
.warn{color:#f5c542;font-weight:bold}
.ok{color:#4ade80}
.info{color:#58a6ff}
.cmd{color:#e8eef5;font-weight:bold}
.dim{color:#5d6673}
.hl{color:#ff9d5c;font-weight:bold}
.plain{color:#c9d1d9}
"""

_CSS_PANEL = """
html,body{margin:0;padding:0;background:#111217;}
.dash{width:1240px;height:690px;background:#111217;box-sizing:border-box;padding:18px 22px;
  font-family:'Microsoft YaHei','Segoe UI',sans-serif;}
.topbar{display:flex;align-items:center;gap:14px;padding-bottom:12px;border-bottom:1px solid #26282e;}
.logo{width:22px;height:22px;border-radius:4px;background:#f2cc0c;display:inline-block;}
.brand{color:#e8eef5;font-size:17px;font-weight:600;}
.env{color:#8e929a;font-size:12px;background:#1d2026;padding:3px 10px;border-radius:10px;}
.grid{display:flex;gap:14px;margin-top:16px;}
.card{flex:1;background:#181b1f;border:1px solid #26282e;border-radius:6px;padding:14px 16px;}
.card .name{color:#9aa1ad;font-size:13px;}
.card .value{font-size:34px;font-weight:700;margin-top:8px;font-family:Consolas,monospace;}
.card .note{color:#6b7280;font-size:12px;margin-top:6px;}
.tblwrap{margin-top:16px;background:#181b1f;border:1px solid #26282e;border-radius:6px;padding:6px 14px 10px;}
table{width:100%;border-collapse:collapse;font-family:Consolas,'Microsoft YaHei',monospace;font-size:13.5px;}
th{color:#8e929a;text-align:left;padding:9px 10px;border-bottom:1px solid #26282e;font-weight:500;}
td{color:#c9d1d9;padding:8px 10px;border-bottom:1px solid #1f2229;white-space:nowrap;}
tr:last-child td{border-bottom:none}
.tag{padding:2px 9px;border-radius:9px;font-size:12px;font-weight:600;}
.firing{background:#3d1a1a;color:#ff5f56;}
.ok-tag{background:#12301c;color:#4ade80;}
.p0{color:#ff5f56;font-weight:700;}
.p1{color:#f5c542;font-weight:700;}
.annot{margin-top:14px;color:#c9d1d9;font-size:13.5px;line-height:1.6;background:#1d2026;
  border-left:3px solid #f2cc0c;padding:10px 14px;border-radius:0 6px 6px 0;}
"""


# =====================================================================
# 舰队扩展：前 42 个舰队服务 ×（终端日志 + 监控面板）= 84 张，合计 104 张。
# 场景由 scripts/screenshot_fleet.py 从 data/source/fleet.py 同源派生，
# 数字与文本语料共用同一套公式（截图里的数字 = 文档里的数字 = 评估锚点）。
# 放在渲染函数之前、SCENES 定义之后：gen_image_transcripts 导入 SCENES
# 时会一并带出舰队场景，转写自动覆盖，无需额外步骤。
# =====================================================================
from scripts.screenshot_fleet import extra_scenes as _fleet_extra_scenes  # noqa: E402

SCENES.extend(_fleet_extra_scenes())


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_terminal(scene: dict) -> str:
    rows = []
    for cls, text in scene["lines"]:
        rows.append(f'<div><span class="{cls}">{esc(text)}</span></div>')
    lines_html = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{_CSS_TERMINAL}</style></head>
<body>
<div class="window">
  <div class="titlebar">
    <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
    <span class="title">{esc(scene["title"])}</span>
  </div>
  <div class="body">{lines_html}</div>
</div>
</body></html>"""


def render_panel(scene: dict) -> str:
    cards = []
    for name, value, color, note in scene["metrics"]:
        cards.append(
            f'<div class="card"><div class="name">{esc(name)}</div>'
            f'<div class="value" style="color:{color}">{esc(value)}</div>'
            f'<div class="note">{esc(note)}</div></div>'
        )
    cards_html = "\n".join(cards)

    rows = []
    for i, row in enumerate(scene["table"]):
        cells = "".join(f"<td>{esc(c)}</td>" for c in row)
        if i == 0:
            header = "".join(f"<th>{esc(c)}</th>" for c in row)
            rows.append(f"<tr>{header}</tr>")
        else:
            tag_html = ""
            for j, c in enumerate(row):
                if c in ("Firing", "OK"):
                    cls = "firing" if c == "Firing" else "ok-tag"
                    tag_html += f'<td><span class="tag {cls}">{esc(c)}</span></td>'
                elif c in ("P0", "P1"):
                    tag_html += f'<td class="{c.lower()}">{esc(c)}</td>'
                else:
                    tag_html += f"<td>{esc(c)}</td>"
            rows.append(f"<tr>{tag_html}</tr>")
    table_html = "\n".join(rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{_CSS_PANEL}</style></head>
<body>
<div class="dash">
  <div class="topbar">
    <span class="logo"></span>
    <span class="brand">运维监控 · 值班总览</span>
    <span class="env">prod · es-prod / ops-monitor</span>
    <span class="env">2026-10-16 10:24 CST</span>
  </div>
  <div class="grid">{cards_html}</div>
  <div class="tblwrap"><table>{table_html}</table></div>
  <div class="annot">{esc(scene["annotation"])}</div>
</div>
</body></html>"""


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    raise FileNotFoundError("未找到 Chrome/Edge，请检查 CHROME_CANDIDATES")


def shoot(chrome: str, html_path: Path, png_path: Path, profile: Path) -> None:
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-sandbox",
        "--no-first-run",
        "--disable-extensions",
        f"--force-device-scale-factor={SCALE}",
        f"--window-size={W},{H}",
        f"--screenshot={png_path}",
        f"--user-data-dir={profile}",
        html_path.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="ignore")
    if not png_path.exists():
        raise RuntimeError(f"截图失败 {png_path.name}: {proc.stdout[-300:]} {proc.stderr[-300:]}")


def shoot_with_retry(chrome: str, html_path: Path, png_path: Path, profile: Path,
                     attempts: int = 3) -> None:
    """Chrome headless 偶发 60s 超时（首启慢 / profile 抖动），重试即可恢复。

    不加重试时批量渲染会因单张抖动整批中断 —— 20 张时代没暴露，
    104 张时代一次真实超时就毁掉整轮生成。
    """
    last: Exception | None = None
    for a in range(attempts):
        try:
            shoot(chrome, html_path, png_path, profile)
            return
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            last = exc
            print(f"    [RETRY {a + 1}/{attempts}] {png_path.name}: {type(exc).__name__}", flush=True)
            time.sleep(2)
    raise last  # type: ignore[misc]


def clean_tmp(tmp_dir: Path) -> tuple[int, int]:
    """清理渲染中间产物（HTML + Chrome profile）。

    为什么必须清：Chrome 的 `--user-data-dir` 会留下上百个缓存文件，
    一次运行就是好几 MB —— 比 20 张正式截图加起来还大。
    生成器不清理自己，这些垃圾就会跟着仓库走。

    实现用逐文件 unlink + 自底向上 rmdir，而不是 shutil.rmtree：
    在会拦截递归删除的环境里前者仍可用，且失败时能精确指出是哪个文件。
    """
    if not tmp_dir.exists():
        return 0, 0

    files = 0
    dirs = 0
    failed: list[str] = []

    for path in sorted(tmp_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                files += 1
            elif path.is_dir():
                path.rmdir()
                dirs += 1
        except OSError as exc:
            failed.append(f"{path}: {exc}")

    try:
        tmp_dir.rmdir()
        dirs += 1
    except OSError as exc:
        failed.append(f"{tmp_dir}: {exc}")

    if failed:
        print(f"  [WARN] 有 {len(failed)} 项未能删除（通常是 Chrome 仍在占用）：")
        for item in failed[:5]:
            print(f"         {item}")
        print("         关闭 Chrome 后重跑，或手动删除该目录。")
    return files, dirs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="只生成指定 id 的截图")
    parser.add_argument("--keep-tmp", action="store_true",
                        help="保留中间产物（HTML 与 Chrome profile），用于排查渲染问题")
    args = parser.parse_args()

    chrome = find_chrome()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    profile = TMP_DIR / "profile"

    manifest: list[dict] = []
    scenes = [s for s in SCENES if not args.only or s["id"] == args.only]

    for scene in scenes:
        html_text = render_panel(scene) if scene.get("kind") == "panel" else render_terminal(scene)
        html_path = TMP_DIR / f"{scene['id']}.html"
        html_path.write_text(html_text, encoding="utf-8")

        png_path = IMG_DIR / f"{scene['id']}.png"
        shoot_with_retry(chrome, html_path, png_path, profile)
        size_kb = png_path.stat().st_size / 1024
        print(f"  [OK] {scene['id']}.png  ({size_kb:.0f} KB)  {scene['caption'][:36]}...")

        manifest.append({
            "image_id": scene["id"],
            "file": png_path.relative_to(ROOT).as_posix(),
            "caption": scene["caption"],
            "related_docs": scene["related"],
            "kind": scene.get("kind", "terminal"),
            "width": W * SCALE,
            "height": H * SCALE,
            "generated": TODAY,
        })

    mpath = IMG_DIR / "MANIFEST.json"
    mpath.write_text(json.dumps({"total": len(manifest), "images": manifest}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    print(f"\n共生成 {len(manifest)} 张截图；清单：{mpath.relative_to(ROOT).as_posix()}")
    print(f"关联文档 {sum(len(m['related_docs']) for m in manifest)} 处（多模态 QA 的检索锚点）")

    if args.keep_tmp:
        print(f"\n保留中间产物：{TMP_DIR.relative_to(ROOT).as_posix()}/")
    else:
        files, dirs = clean_tmp(TMP_DIR)
        freed = f"，清理 {files} 个文件 / {dirs} 个目录" if files or dirs else ""
        print(f"\n中间产物已清理{freed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
