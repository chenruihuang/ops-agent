"""应用配置。所有可调参数集中在 .env，代码里不出现魔法数字。"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- LLM ----------
    deepseek_api_key: SecretStr = SecretStr("sk-placeholder")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ---------- 检索 ----------
    # 向量库后端：qdrant（默认）| chroma | local
    # 切换后端只需改这一行 —— 见 app/ingest/stores.py 的选型说明与接口契约。
    vector_backend: str = "qdrant"
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "ops_kb"
    qdrant_distance: str = "Cosine"
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    # 非空则用嵌入式 PersistentClient（单机/离线）；空则用 HttpClient
    chroma_persist_dir: str = ""
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    reranker_model: str = "BAAI/bge-reranker-base"
    top_k_retrieve: int = 20
    top_k_rerank: int = 5
    # 多路召回融合开关。关掉就退化成"向量单路按分数排序"，
    # 用于对照实验 —— 没有这个开关，"RRF 到底有没有用"就只能靠嘴说。
    recall_fusion_enabled: bool = True
    # 候选池里同一文档最多占几个坑位。
    #
    # 这个上限不是调优参数，是**正确性约束**：长文档会被切成几十块，
    # 若它恰好与问题相关，前 20 个候选可能全被它占满，另外几篇真正需要的
    # 文档连池子都进不去 —— 重排再强也救不回来。
    # 取 3：一个文档通常只需要 1~2 块（现象 + 处置），3 已足够容错，
    # 同时保证池子能容纳 6 篇以上不同文档。
    recall_max_per_doc: int = 3
    # 重排后最终上下文里同一文档最多几块。
    # 取 2：top_k_rerank=5 时，至少能覆盖 3 篇文档，
    # 避免"答案只出自一篇文档"这种看起来正常、实则视野很窄的结果。
    rerank_max_per_doc: int = 2
    # 重排总开关。
    #
    # 存在的理由不是"留个可选项"，而是实测逼出来的：
    # CrossEncoder 在纯 CPU 上是本链路最贵的一环 —— 候选池 20 时单题约 29.6s
    # （scripts/bench_rerank.py，机器空闲）。这个量级会让两件事失效：
    #   1) 3 分钟演示 —— 每问一句要等半分钟，演示节奏直接崩
    #   2) 压测的 TTFT 指标 —— TTFT 被重排支配，测到的是重排的延迟，
    #      不是"这套检索架构的延迟"
    # 所以必须能一键关掉，关掉后走"融合序 + 文档多样性约束"（与重排失败
    # 时的降级路径**同一条**，不是另写一份）。
    #
    # 默认 True 保持功能完整；跑压测/演示时在 .env 里设 RERANK_ENABLED=false，
    # 并必须在报告里注明这一点 —— 否则等于用一个更弱的链路冒充完整链路。
    rerank_enabled: bool = True

    # ---------- 意图路由 ----------
    # 低置信度时是否用 LLM 兜底分类。默认关：
    #   1) 缓存命中路径 TTFT 是毫秒级，插一次 LLM 分类会把它毁掉
    #   2) 规则路由在 52 条标注上是可评估的（scripts/check_intent.py），
    #      先有基线才知道 LLM 兜底值不值那 500ms
    intent_llm_fallback: bool = False
    # 低于该置信度才考虑兜底（只有开上面那个开关才生效）
    intent_llm_threshold: float = 0.7

    # ---------- 工具 ----------
    # 单个工具的超时。取 3s：工具是辅助信息，不该比一次向量检索还慢。
    tool_timeout_seconds: float = 3.0

    # ---------- 缓存 ----------
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    # RESP 协议版本。**保持 2，不要改成 3。** 两个原因，都是踩过的坑：
    #
    # 1) redis-py >= 6 的默认协议是 3（见 connection.DEFAULT_RESP_VERSION），
    #    连接时会先发 `HELLO 3`。Redis 6.0 以下不认识这个命令，直接握手失败：
    #        ResponseError: unknown command 'HELLO'
    #    实测本机 `D:\Program Files\Redis` 的 3.0.504 就会这样。
    #
    # 2) RediSearch 在两种协议下返回结构不同 —— 这不是风格差异，是结构差异：
    #        RESP2: [total, "key:1", ["f", "hello"], ...]        ← 扁平数组
    #        RESP3: [total, {"results": [...], "extra_attributes": ...}]
    #    而 `RedisCache._parse_search_reply` 是按扁平数组写的。走 RESP3 时
    #    reply 长度变成 2，while 循环一次都进不去 → **返回空候选**，
    #    表现为「语义缓存永远 miss」且只有一条 warning，极难排查。
    #
    # 所以 2 不是保守选择，而是当前解析代码的正确前提。
    # 要改 3 必须同时重写 _parse_search_reply 并补 RESP3 的回归测试。
    redis_protocol: int = 2
    cache_exact_ttl: int = 86_400
    cache_semantic_ttl: int = 21_600
    cache_semantic_threshold: float = 0.96
    cache_rewrite_ttl: int = 86_400
    cache_rerank_ttl: int = 21_600
    cache_tool_ttl: int = 30
    cache_ttl_jitter: float = 0.1
    cache_null_ttl: int = 60
    cache_enabled: bool = True
    # 单飞（singleflight）：同题并发时只放一个请求进后端
    # 注意这是"成本与后端保护"的取舍，不是延迟优化 —— 见 README 里的说明
    cache_singleflight_enabled: bool = True
    cache_singleflight_wait: float = 6.0     # 跟随者最多等多久，超时自己算
    cache_singleflight_hold: int = 30        # 锁的最长持有时间（秒）

    # ---------- 版本号：缓存失效总开关 ----------
    kb_version: int = 1
    prompt_version: str = "v1"

    # ---------- 知识库入库 ----------
    kb_raw_dir: str = "data/raw"                  # 语料根目录（相对仓库根）
    kb_index_dir: str = "data/index"              # 索引产物目录
    ocr_provider: str = "transcript"              # transcript | rapidocr
    caption_provider: str = "rule"                # rule | vision
    vision_model: str = "deepseek-chat"           # 多模态描述用的模型
    chunk_max_chars: int = 460                    # 上限倒推自 embedding 模型 512 token
    chunk_merge_min_chars: int = 80
    chunk_overlap_chars: int = 80
    ingest_batch_size: int = 64                   # 向量库 upsert 批大小
    ingest_embed_batch: int = 16                  # embedding 批大小

    # ---------- 外部系统 ----------
    mcp_server_url: str = "http://java-mcp:8080"
    mcp_enabled: bool = True

    # ---------- 韧性（熔断 / 重试 / 降级）----------
    # 顺序是 breaker(外) → retry(内)：只有"重试用尽仍失败"才计一次熔断失败。
    # 反过来会让 3 次重试打满阈值 —— 抖一下就熔断。
    #
    # 重试次数：MCP 是内网 HTTP，1 次重试能吃掉绝大多数瞬时抖动；
    # 3 次是"抖动 + 一次 GC 停顿"的上限。再多只是在放大尾延迟。
    mcp_retry_max_attempts: int = 3
    mcp_retry_base_delay: float = 0.1      # 退避起点 100ms → 200ms → 400ms（带 20% 抖动）
    mcp_retry_max_delay: float = 1.0
    mcp_retry_jitter: float = 0.2
    # MCP 单次请求超时。**必须满足 timeout × 重试次数 + 退避 < tool_timeout_seconds**，
    # 否则 ToolRegistry 会在重试走完前切断整个调用（表现为"日志在重试、实际已超时"）。
    # 0.8 × 3 + ~0.3 ≈ 2.7s < 3s。设 0 表示按公式自动倒算。
    mcp_timeout_seconds: float = 0.8
    # 熔断阈值：连续 5 次"最终失败"才开。取 5 而不是 3 ——
    # 并发 2 个请求各重试 3 次时，3 容易被一次故障放大成误熔断。
    mcp_cb_fail_threshold: int = 5
    # 冷却 15s：够下游重启一次；太短会在恢复期反复开合（flapping）。
    mcp_cb_cool_down: float = 15.0
    # 半开只放 1 个探测：恢复瞬间积压的请求全放行会把刚起来的下游再打垮。
    mcp_cb_half_open_max: int = 1
    # 故障注入：非空时作为 X-Chaos 头发给 Java MCP（见 java-mcp/ChaosFilter）。
    # 只在演示/自测时用，生产留空。**默认空**是安全边界，不是省略。
    mcp_chaos: str = ""
    # skill 声明文件（YAML）。不存在/解析失败时退回代码内置定义。
    skills_path: str = "app/tools/skills.yaml"

    # ---------- 安全沙箱（写操作人工确认）----------
    # 总开关。关掉之后写工具**不是变成直接执行**，而是全部被拒绝（fail-closed）——
    # 开关的语义是"更严"而不是"放行"，否则线上一次误操作就能把闸门关掉。
    sandbox_enabled: bool = True
    # 待确认动作的存活时间。10 分钟：够一个人看到消息、切回来点确认；
    # 太长（比如 1 天）会让"三天前那条确认"在系统里继续有效 —— 上下文早变了。
    sandbox_pending_ttl: int = 600
    # 审计日志。只记写/危险动作与被拒绝的调用，只读工具不记（traces 里已有）。
    sandbox_audit_path: str = "data/audit/sandbox.jsonl"

    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: SecretStr = SecretStr("root123")
    mysql_database: str = "ops_ticket"

    # ---------- 可观测（追踪）----------
    # 三个开关的分工必须分清，否则会得出错误结论：
    #   observability_enabled  —— 埋点总闸（关掉 = 不建 span、不落盘）
    #   observability_trace_dir —— 本地 JSONL 明细目录（**全量**，不采样）
    #   langfuse_enabled        —— 是否上报云端（还需 public/secret key）
    # 本地全量 + 云端采样的方向是刻意的：本地是"回放某次失败请求"的事实源，
    # 一旦本地也采样，回放就变成碰运气。
    observability_enabled: bool = True
    observability_trace_dir: str = "data/traces"
    observability_sample_rate: float = 1.0   # 只作用于 Langfuse 上报
    observability_tags: str = "ops-agent"    # 逗号分隔；Langfuse 里做筛选维度
    app_release: str = ""
    app_environment: str = "default"

    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # 连不上时的单次请求超时。取 5s 是"够慢网络握手"与"别把关机拖成 1 分钟"的折中；
    # SDK 内部有重试，实测连不上时 flush() 约 0.9s 返回。
    langfuse_timeout: float = 5.0
    # 构造客户端的等待上限。实测同一份代码在 Windows 宿主上构造耗时
    # 10~51s 不等（SDK 5.0.1 + 网络质量），容器内 5.0.2 是 0.6s。
    # 超过这个值就放弃上报，本地 JSONL 不受影响 —— 启动时间不该被一个
    # 可选依赖拖成"服务起不来"的样子。
    langfuse_init_timeout: float = 10.0
    langfuse_flush_at: int = 64
    langfuse_flush_interval: float = 2.0

    # ---------- 应用 ----------
    log_level: str = "INFO"
    api_key: SecretStr = SecretStr("demo-key-change-me")
    rate_limit_per_minute: int = 120
    request_timeout_seconds: float = 30.0
    max_concurrency: int = 32

    # ---------- 三层记忆（会话 + 长期事实，工作记忆 = LangGraph state） ----------
    # 开关放在最前：记忆是"锦上添花"的依赖，出问题时第一反应应该是整层关掉，
    # 而不是去查 Redis。压测脚本也用它把记忆层排除在测量之外。
    session_memory_enabled: bool = True
    session_ttl_seconds: int = 86400     # 会话历史 24h：过夜即失效
    session_max_turns: int = 10          # 注入 prompt 的最近轮数（滑窗，不是日志）
    facts_ttl_seconds: int = 7 * 86400   # 长期事实 7 天：背景比对话活得久
    facts_max: int = 20                  # 每会话长期事实上限（防 prompt 膨胀）

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
