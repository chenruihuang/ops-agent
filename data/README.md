# 语料与评估数据说明

> 本目录的数据由 `data/source/`（结构化知识源）**同源派生** —— 文档、截图、QA 标注
> 三者出自同一份事实数据，不存在相互矛盾的可能。改语料请改 source，然后重新生成。

## 场景设定

企业级电商交易系统的 IT 运维知识库：12 个微服务（订单 / 支付 / 库存 / 风控 / 搜索等）、
23 个故障处置 Runbook、15 条告警规则、变更规范与值班流程。
所有数值（端口、阈值、错误码、命令）都是可检索的硬事实。

## 数据规模

| 类别 | 数量 | 位置 |
|---|---|---|
| 服务手册 | 12 | `raw/services/` |
| 错误码字典 | 12 | `raw/errors/` |
| 故障 Runbook | 23 | `raw/runbooks/` |
| 告警规则 | 15 | `raw/alerts/` |
| 流程规范 | 7 | `raw/policies/` |
| 架构与容量 | 7 | `raw/arch/` |
| 值班与流程 | 7 | `raw/oncall/` |
| 可观测规范 | 6 | `raw/observability/` |
| 命令手册 | 8 | `raw/commands/` |
| FAQ | 8 | `raw/faq/` |
| 长文档·操作指南 | 2 | `raw/guides/`（压测切块用，单篇 5000+ 字符） |
| 长文档·技术规范 | 2 | `raw/specs/` |
| 长文档·故障复盘 | 1 | `raw/postmortems/` |
| **文档合计** | **110 篇 / 约 11.7 万字符** | `raw/`（清单 `raw/MANIFEST.json`） |
| 运维截图 | 20 张（2480×1380，OCR 友好） | `raw/images/` |
| QA 标注集 | 52 条（五维检索覆盖 + 长文档专项 12 条） | `../eval/questions.json` |
| 工具 mock 数据 | 6 工单 + 3 资产 | `../eval/mock_records.json` |

> 那 5 篇长文档（`guides/` `specs/` `postmortems/`）不是"多凑点料"：
> 原有语料形态太单一（零个三级标题、最长 section 550 字符），切块器的窗口切分、
> 超长表格拆行、深层标题归属三条分支在真实语料上零覆盖。补完之后真实抓出
> 3 个切块器 bug，详见根目录 README 的 §8.7。

## 评估集设计（52 条）

| 维度 | 条数 | 考察什么 |
|---|---|---|
| single-hop 单跳事实 | 12 | 基础召回：错误码 / 阈值 / 参数 |
| multi-hop 跨文档推理 | 7 | 召回完整性：需同时命中 2-3 篇 |
| colloquial 口语化 | 6 | query rewrite：用户不说术语 |
| edge 边界与否定 | 4 | 区分度：相似概念不能混（穿透 vs 雪崩） |
| multimodal 多模态 | 4 | 截图-文档跨模态检索 |
| longdoc 长文档专项 | 12 | 跨块事实完整性：超长表格 / 深层标题 / 长文关键词稀释 |
| tool / hybrid / chitchat | 7 | 意图路由：3 + 3 + 1 |

难度分布：easy 14 / medium 19 / hard 19。26 条含多个 golden 文档（考验 rerank 区分度）。

## 截图怎么来的（面试可讲的点）

用 **HTML + Chrome headless 截图**（`--headless=new --screenshot`）生成：
浏览器真实渲染，中文 / 颜色 / 等宽字体 100% 精确可 OCR。
刻意不用 AI 生图 —— AI 生图文字必然乱码，OCR 评估环节就失去意义了。
两类形态：终端日志风（18 张）与 Grafana 监控面板风（2 张）。

## 修改语料的正确流程

```bash
# 1. 改事实数据（不要直接改生成的 md —— 会被下次生成覆盖）
vim data/source/services.py          # 或 runbooks.py / policies_arch.py / operations.py
vim data/source/longform.py          # 长文档（切块压力测试用）

# 2. 校验引用网络（悬空引用、命名、内容质量、frontmatter）
python -m scripts.check_corpus

# 3. 重新生成文档 + 清单
#    删除过文档时必须加 --clean，否则被删文档的旧 md 会留在 raw/ 里继续被入库
python -m scripts.gen_corpus --clean

# 4. 改 QA 标注（如需要）并重新生成评估集
python -m scripts.gen_eval
python -m scripts.check_annotation eval/questions.json
```

> **新增/删除文档 id 时要注意**：文档 id 的前缀集合写在 3 个地方，必须同步改 ——
> `scripts/corpus_build.py` 的 `REF_PATTERN`、`app/ingest/chunker.py` 的 `_DOC_ID_TOKEN`、
> `scripts/check_corpus.py` 的名称正则。三处都留了同步警告注释。
> 不同步的后果是"引用识别不到"或"把子串误判成文档 id"（曾经发生：
> `svc-search-service` 里的子串被当成不存在的 `arch-service`，凭空多出一条悬空图谱边）。

## 与指标的关系（诚实红线）

- `context recall@5` 的分母 = `golden_doc_ids` 命中数。**标注错了指标就错，而且看起来正常** ——
  所以 `gen_eval.py` 在写入前强制校验 golden 引用存在性。
- 多模态题（4 条）需要把截图 OCR 文本与文档共同索引后才能命中，是「多模态知识库」
  环节有效性的直接证据。
- 本地单机测出的数字必须标注测量条件（并发、数据集规模、单机配置），
  禁止写成生产 SLA —— 见 `raw/arch/arch-sla.md` 的「单机测试的诚实边界」。
