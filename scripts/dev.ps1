param([Parameter(Position = 0)][string]$cmd = "help")

# ops-agent 一键操作入口。保持与 cs-agent 的 dev.ps1 一致的使用风格 ——
# 两个项目交付风格统一，本身就是"我懂交付"的信号。

$compose = "docker compose"

switch ($cmd) {
  "up"        { $compose up -d }
  "build"     { $compose up -d --build }
  "down"      { $compose down }
  "clean"     { $compose down -v; Write-Host "已清空数据卷（向量库/缓存/MySQL 全部重置）" }
  "logs"      { $compose logs -f api }
  "ps"        { $compose ps }
  "sky"       { $compose up -d --scale api=3; Write-Host "已扩展到 3 副本，nginx 自动负载均衡" }

  "kb"        { $compose run --rm build-kb }
  "eval"      { $compose run --rm eval }
  "test"      { $compose run --rm api python -m pytest tests -v }

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
    param([string]$q = "诉讼时效是几年")
    curl.exe -N -s -X POST "http://localhost:8080/chat/stream" `
      -H "Content-Type: application/json" `
      -H "X-API-Key: demo-key-change-me" `
      -d "{`"message`":`"$q`"}"
  }

  "load"      { $compose --profile loadtest up -d locust; Write-Host "Locust UI: http://localhost:8089" }
  "load-ci"   { $compose --profile loadtest run --rm locust locust -f /app/loadtest/locustfile.py --host http://nginx --headless --users 120 --spawn-rate 5 --run-time 6m --html /app/loadtest/report.html }
  "fault"     { python -m scripts.fault_inject --fault $(if ($args[0]) { $args[0] } else { "stop-qdrant" }) }

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
    Write-Host "  数据/评估 : kb | eval | test | check"
    Write-Host "  本地(无 Docker) : kb-build | kb-index | eval-bm25 | eval-local | gen-corpus"
    Write-Host "  演示      : bump | stats | ask | urls"
    Write-Host "  压测      : load | load-ci | fault"
    Write-Host ""
    Write-Host "  kb-build  : 切块 + 建索引产物（秒级，无需模型与容器）"
    Write-Host "  eval-bm25 : 关键词路召回率，当前唯一能离线产出的真实指标"
  }
}
