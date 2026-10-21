param(
  [Parameter(Position = 0)][string]$cmd = "help",
  # 多余的位置参数收进来：`dev.ps1 ask "如何排查 502"` 里的中文问题。
  # 不加这一行，PS 会直接报 "A positional parameter cannot be found..."，
  # 脚本根本不执行 —— 而报错看起来像"命令用法不对"，很容易误判成命令不存在。
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$rest = @()
)

# ops-agent 一键操作入口。保持与 cs-agent 的 dev.ps1 一致的使用风格 ——
# 两个项目交付风格统一，本身就是"我懂交付"的信号。


Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
switch ($cmd) {
  "up"        { docker compose up -d }
  "build"     { docker compose up -d --build }
  "down"      { docker compose down }
  "clean"     { docker compose down -v; Write-Host "已清空数据卷（向量库/缓存/MySQL 全部重置）" }
  "logs"      { docker compose logs -f api }
  "ps"        { docker compose ps }
  "sky"       { docker compose up -d --scale api=3; Write-Host "已扩展到 3 副本，nginx 自动负载均衡" }

  "kb"        { docker compose run --rm build-kb }
  "eval"      { docker compose run --rm eval }

  # 模型缓存：把宿主已下载的 HF 缓存灌进共享卷。
  # 为什么需要这条：api 容器带 HF_HUB_OFFLINE=1（见 docker-compose.yml），而 huggingface_cache
  # 卷是空的 —— warmup 的异常被 lifespan 接住，容器照样 healthy，模型却永远不可用。
  # 症状是 /ready 里 embedding_model.ok=false，日志里 "We couldn't connect to 'https://hf-mirror.com'"。
  "models"    {
    $src = (Join-Path $env:USERPROFILE ".cache\huggingface\hub").Replace("\", "/")
    if (-not (Test-Path -LiteralPath $src)) {
      throw "宿主没有 HF 缓存: $src —— 先跑一次 .\scripts\dev.ps1 kb（build-kb 允许联网下载）"
    }
    $img = (docker compose images -q api | Select-Object -First 1)
    if (-not $img) { throw "找不到 api 镜像，先跑 .\scripts\dev.ps1 build" }
    # 注意层级：卷挂的是 /root/.cache/huggingface，缓存本体在它下面的 hub/ ——
    # 直接铺到卷根目录会变成 /root/.cache/huggingface/models--BAAI--*，HF 找不到，白拷一次。
    docker run --rm --entrypoint sh -v ops-agent_huggingface_cache:/dst -v "${src}:/src:ro" $img -c "mkdir -p /dst/hub && cp -a /src/. /dst/hub/ && du -sh /dst/hub && ls /dst/hub"
    Write-Host ""
    Write-Host "已灌入 ops-agent_huggingface_cache。下一步："
    Write-Host "  docker compose restart api"
    Write-Host "  curl.exe -s http://localhost:8080/ready    # 期望 ready=true、embedding_model ok=true"
  }
  # D1 接口级自测：走 selftest.ps1（它会先切 65001，否则中文全乱码）。
  # 直接 `python -m scripts.selftest` 也能跑，只是本机 936 代码页下会看到
  # `D1 鑷祴` 这种输出 —— 这不是测试失败，但极容易被误读成失败。
  #
  # 不转发 $rest：数组 splat 是按位置传的，`-Only stream` 会被拆成 "stream"
  # 之类的位置参数而被错误绑定。要选场景就直接跑 .\scripts\selftest.ps1 -Only stream。
  "selftest"  {
    if ($rest.Count -gt 0) {
      Write-Host ("（参数未转发）要选场景请直接跑：.\scripts\selftest.ps1 " + ($rest -join " ")) -ForegroundColor Yellow
    }
    & (Join-Path $PSScriptRoot "selftest.ps1")
  }
  "test"      { docker compose run --rm api python -m pytest tests -v }

  # ---- 追踪回放：不依赖 Langfuse，读本地 data/traces/*.jsonl ----
  # 默认最近 1 轮；加参数走 $rest，例如：
  #   .\scripts\dev.ps1 trace --list-sessions
  #   .\scripts\dev.ps1 trace --session selftest-6a6b9d39
  "trace"     { python -m scripts.trace_replay @rest }

  # ---- 追踪自检 / 埋点开销：都只在宿主机跑，不碰容器 ----
  #   .\scripts\dev.ps1 trace-doctor          配置 + 目录可写 + host 可达
  #   .\scripts\dev.ps1 trace-doctor --send-test
  #   .\scripts\dev.ps1 trace-bench --rounds 500
  "trace-doctor" { python -m scripts.trace_doctor @rest }
  "trace-bench"  { python -m scripts.bench_trace @rest }

  # ---- 入库：本地跑（不需要 Docker，只有 build 阶段）----
  # 切块策略调一次就要重跑一轮评估，如果每轮都等容器起停，一天改不了几次。
  "kb-build"  { python -m scripts.ingest --stage build }
  "kb-index"  { python -m scripts.ingest --stage index --backend local }
  "eval-bm25" { python -m scripts.eval_retrieval --backend bm25 --sweep }
  "eval-local"{ python -m scripts.eval_retrieval --backend local --sweep }
  "gen-corpus"{ python -m scripts.gen_corpus; python -m scripts.gen_image_transcripts; python -m scripts.gen_eval }
  "check"     { python -m scripts.check_corpus; python -m scripts.check_annotation eval/questions.json }

  "bump"      { Invoke-RestMethod -Method Post -Uri "http://localhost:8080/admin/kb/bump" -Headers @{ "X-API-Key" = "demo-key-change-me" } | ConvertTo-Json }
  "stats"     { Invoke-RestMethod -Uri "http://localhost:8080/admin/cache/stats" -Headers @{ "X-API-Key" = "demo-key-change-me" } | ConvertTo-Json }
  "ask"       {
    # 问题从 $rest 拿（见文件头的 param 说明）；不写默认值就取 VPN —— 注意
    # 语料里没有 VPN 内容，答案会是"资料未涉及"，想看长回答请显式传问题。
    $q = if ($rest.Count -gt 0) { $rest -join " " } else { "VPN 连不上怎么办" }
    # 不要内联 -d "{...}"：PS 5.1 会把 JSON 的双引号当字符串结束符吃掉，中文还会被截断
    # （实测表见 scripts/selftest.py 开头）。落盘成 UTF-8(无 BOM) 再 --data-binary @file，
    # 命令行里只出现 ASCII 文件名 —— 这是唯一稳定的传参方式。
    $body = @{ message = $q; use_cache = $false } | ConvertTo-Json -Compress
    $tmp = Join-Path $env:TEMP "ops-ask.json"
    [IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding $false))
    curl.exe -N -s -X POST "http://localhost:8080/chat/stream" `
      -H "Content-Type: application/json" `
      -H "X-API-Key: demo-key-change-me" `
      --data-binary "@$tmp"
  }

  "load"      { docker compose --profile loadtest up -d locust; Write-Host "Locust UI: http://localhost:8089" }
  "load-ci"   { docker compose --profile loadtest run --rm locust locust -f /app/loadtest/locustfile.py --host http://nginx --headless --users 120 --spawn-rate 5 --run-time 6m --html /app/loadtest/report.html }
  "fault"     { python -m scripts.fault_inject --fault $(if ($rest.Count -gt 0) { $rest[0] } else { "stop-qdrant" }) }

  "urls" {
    Write-Host "API      : http://localhost:8080/docs"
    Write-Host "Qdrant   : http://localhost:6333/dashboard"
    Write-Host "Grafana  : http://localhost:3001  (admin / admin123)"
    Write-Host "Prometheus: http://localhost:9090"
    Write-Host "Locust   : http://localhost:8089  (需先执行 .\scripts\dev.ps1 load)"
  }

  default {
    Write-Host "用法: .\scripts\dev.ps1 <命令>"
    Write-Host ""
    Write-Host "  启动/停止 : up | build | down | clean | logs | ps | sky"
    Write-Host "  数据/评估 : kb | models | eval | test | check"
    Write-Host "  本地(无 Docker) : kb-build | kb-index | eval-bm25 | eval-local | gen-corpus"
    Write-Host "  演示      : bump | stats | ask | urls"
    Write-Host "  追踪      : trace  （最近 1 轮；--session / --last / --md 见 --help）"
    Write-Host "              trace-doctor （配置/落盘/连通自检，--send-test 真发一个 span）"
    Write-Host "              trace-bench  （埋点开销：--rounds 500，报口径别只报数字）"
    Write-Host "  自测      : selftest  （选场景/存日志用 .\scripts\selftest.ps1 -Only stream -Log x.log）"
    Write-Host "  压测      : load | load-ci | fault"
    Write-Host ""
    Write-Host "  kb-build  : 切块 + 建索引产物（秒级，无需模型与容器）"
    Write-Host "  models    : 宿主 HF 缓存 → huggingface_cache 卷（/ready 报 embedding ✗ 时用）"
    Write-Host "  eval-bm25 : 关键词路召回率，当前唯一能离线产出的真实指标"
  }
}
} finally {
  Pop-Location
}
