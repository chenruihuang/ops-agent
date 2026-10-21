"""答案侧评估跑批：把标注集喂给**真实链路**，算 RAGAS 口径的确定性指标。

跑法
----
    # 先小样本确认链路通（8 题）
    python -m scripts.run_eval --limit 8

    # 全量（48 题有 golden，4 题无 golden 只判弃答）
    python -m scripts.run_eval --out eval_output/answers_full.json

    # 指到容器里跑（容器内 host 是 nginx 服务名）
    python -m scripts.run_eval --api http://nginx

口径红线
--------
1. **走的是 `/chat/stream` 同一条线上链路**（含缓存、记忆、意图路由），
   不是另写一个"只检索不生成"的评估路径 —— 否则得到的是"某个别的系统"的分数
   （这条教训在 `eval_matrix` 上已经付过一次代价）。
2. `context_recall` 是**文档级**，与检索评估同口径；不与 RAGAS 的要点级数字互比。
3. `anchor_hit_rate` 只回答"标注的关键事实有没有出现在答案里"，**不是** faithfulness。
   真正的 faithfulness 需要 LLM 判定，本环境装不上 ragas，宁可留空也不编数。
4. `--limit` 是**抽样**，产物里写 `limited_sample: true`，报告时必须一起说。
5. `use_cache=false`：缓存命中会让 TTFT 好看得离谱，且答案来自上一次运行 ——
   评估要测"冷启动路径"，不是测缓存。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from app.eval.answer_metrics import (
    abstains,
    anchor_hit_rate,
    doc_precision,
    doc_recall,
    summarize,
)

DEFAULT_API = "http://localhost:8080"
DEFAULT_QUESTIONS = Path("eval/questions.json")
DEFAULT_OUT_DIR = Path("eval_output")

# `rerank:disabled` 是配置的**必然结果**（RERANK_ENABLED=false），不是故障。
# 事件层已经改成 `notice`（不再冒充 degraded），这里保留是为了**重打分旧产物**时
# 不把历史数据里的这条误判成故障 —— 口径变了，历史产物也得能被正确解读。
TOLERATED_DEGRADED = {"rerank:disabled"}


def doc_id_of(source: str) -> str:
    """docs 帧里的 source 是文件路径（`data/raw/errors/err-x.md`），
    而 golden 是文档 id（`err-x`）。不换算的话 recall 恒为 0 ——
    而且这个 0 看起来"很有说服力"，最容易被当成真实结论。
    """
    name = str(source).replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _api_key(cli: str | None) -> str:
    key = cli or os.environ.get("API_KEY", "")
    if key:
        return key.strip()
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def ask(client: httpx.Client, question: str, *, timeout: float) -> dict[str, Any]:
    """打一次问答，收集 docs / token / degraded / done。**永不抛**（评估跑批要能继续）。"""
    out: dict[str, Any] = {
        "answer": "", "sources": [], "degraded": False, "degraded_reasons": [],
        "ttft_ms": None, "total_ms": None, "status": 0, "error": "",
    }
    started = time.perf_counter()
    first_token_at: float | None = None
    try:
        with client.stream(
            "POST", "/chat/stream",
            json={"message": question, "use_cache": False},
            headers={"Accept": "text/event-stream"},
            timeout=timeout,
        ) as resp:
            out["status"] = resp.status_code
            if resp.status_code >= 400:
                out["error"] = f"HTTP {resp.status_code}"
                return out
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                body = line[6:].strip()
                if body == "[DONE]":
                    break
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "docs":
                    out["sources"] = [doc_id_of(s) for s in (event.get("sources") or [])]
                elif kind == "token":
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    # 字段名是 `content`（见 orchestrator 的 token 帧）。
                    # 早期这里按 `text`/`token` 取值 → 答案恒为空、指标恒为 0，
                    # 而链路本身完全正常 —— 这类"评估器自己的 bug"最难发现。
                    out["answer"] += str(event.get("content") or "")
                elif kind == "degraded":
                    reason = str(event.get("reason") or "")
                    if reason not in TOLERATED_DEGRADED:
                        out["degraded"] = True
                        out["degraded_reasons"].append(reason)
                elif kind == "done":
                    out["total_ms"] = event.get("total_ms")
    except Exception as exc:  # noqa: BLE001 - 单题失败不该中断整批
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["ttft_ms"] = None if first_token_at is None else round(
        (first_token_at - started) * 1000, 1
    )
    if out["total_ms"] is None:
        out["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


def score_one(spec: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    golden = spec.get("golden_doc_ids") or []
    anchors = spec.get("answer_anchors") or []
    answer = got.get("answer") or ""
    hit_rate, hit, missed = anchor_hit_rate(answer, anchors)
    return {
        "id": spec.get("id"),
        "intent": spec.get("intent"),
        "question": spec.get("question"),
        "answer": answer,
        "answer_chars": len(answer),
        "sources": got["sources"],
        "golden_doc_ids": golden,
        "context_recall": doc_recall(golden, got["sources"]),
        "context_precision": doc_precision(golden, got["sources"]),
        "anchor_hit_rate": hit_rate,
        "anchors_hit": hit,
        "anchors_missed": missed,
        "abstained": abstains(answer),
        "degraded": got["degraded"],
        "degraded_reasons": got.get("degraded_reasons", []),
        "ttft_ms": got["ttft_ms"],
        "total_ms": got["total_ms"],
        "status": got["status"],
        "error": got["error"],
    }


def rescore(args: argparse.Namespace) -> int:
    """对已保存的答案**重新打分**，不再调用模型。

    为什么需要它：指标代码改了（比如 AP 的去重修法）时，重跑一遍全量要几分钟
    加一次模型费用，而且答案会变 —— 那就分不清"指标变了"还是"模型这次答得不一样"。
    重打分固定住答案，只让口径变，两者的差异才是可归因的。
    """
    data = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
    specs = {q["id"]: q for q in json.loads(Path(args.questions).read_text(encoding="utf-8"))}
    if args.limit and args.limit > 0:
        data["rows"] = data["rows"][: args.limit]

    rows: list[dict[str, Any]] = []
    for saved in data.get("rows", []):
        spec = specs.get(saved.get("id"), {})
        merged = {
            "id": saved.get("id"),
            "intent": saved.get("intent"),
            "question": saved.get("question") or spec.get("question", ""),
            "golden_doc_ids": saved.get("golden_doc_ids") or spec.get("golden_doc_ids") or [],
            "answer_anchors": spec.get("answer_anchors") or [],
        }
        got = {
            "answer": saved.get("answer") or "",
            "sources": saved.get("sources") or [],
            "degraded": bool(saved.get("degraded")),
            "degraded_reasons": saved.get("degraded_reasons") or [],
            "ttft_ms": saved.get("ttft_ms"),
            "total_ms": saved.get("total_ms"),
            "status": saved.get("status", 0),
            "error": saved.get("error", ""),
        }
        rows.append(score_one(merged, got))

    report = summarize(rows)
    report.update({
        "mode": "rescore",
        "judge": "offline-deterministic",
        "note": "对已保存答案重打分（未重新生成）；context_* 为文档级；anchor_hit_rate 不等于 RAGAS faithfulness",
        "source_artifact": str(args.rescore),
        "limited_sample": bool(args.limit and args.limit < len(rows)),
        "questions_file": str(args.questions),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": rows,
    })
    out = Path(args.out) if args.out else Path(args.rescore).with_name(
        Path(args.rescore).stem + "_rescored.json"
    )
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("重打分完成（答案未变，只有口径变了）")
    for name in ("context_recall", "context_precision", "anchor_hit_rate"):
        print(f"  {name:<18} {report.get(name)}  (n={report.get(f'{name}_n')})")
    print(f"  产物：{out}")

    if report["limited_sample"]:
        print("  ⚠ limited_sample=true —— 抽样结果，不写大盘")
    elif args.publish:
        # 重打分**确实**改了口径（例如 AP 去重修好后 1.3133 → 0.7841），
        # 所以它也需要能把修好的数刷到面板上；否则面板会一直挂着旧口径的数。
        key = _api_key(args.key)
        if not key:
            print("  [WARN] 找不到 API_KEY，跳过上报")
        else:
            publish_report(report, api=args.api, key=key, top_k=args.top_k)
    return 0


def publish_report(report: dict[str, Any], *, api: str, key: str, top_k: int,
                   timeout: float = 20.0) -> bool:
    """把汇总写进 api 进程的 registry（`POST /admin/eval/metrics`），供 Prometheus 抓取。

    只传算出来的项：`_n=0` 的指标（例如没有任何题带 golden）传 None，
    让端点"跳过"而不是写入 0 —— 大盘上"没测过"和"测出来是 0"必须能分开。
    """
    payload: dict[str, Any] = {"top_k": top_k}
    for name in ("context_recall", "context_precision", "anchor_hit_rate"):
        payload[name] = report.get(name) if report.get(f"{name}_n") else None
    try:
        with httpx.Client(base_url=api, headers={"X-API-Key": key},
                          timeout=timeout, trust_env=False) as client:
            resp = client.post("/admin/eval/metrics", json=payload)
    except httpx.HTTPError as exc:
        print(f"  [WARN] 上报大盘失败（{type(exc).__name__}: {exc}）—— 产物已落盘，指标未更新")
        return False
    if resp.status_code >= 400:
        print(f"  [WARN] 上报被拒 HTTP {resp.status_code}：{resp.text[:200]}")
        return False
    body = resp.json()
    print(f"  已上报大盘 top_k={body.get('top_k')} written={body.get('written')} "
          f"skipped={body.get('skipped')}")
    return True


def run(args: argparse.Namespace) -> int:
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    if args.only_intent:
        wanted = {x.strip() for x in args.only_intent.split(",") if x.strip()}
        questions = [q for q in questions if q.get("intent") in wanted]
    if args.limit and args.limit > 0:
        questions = questions[: args.limit]

    key = _api_key(args.key)
    if not key:
        print("[FATAL] 找不到 API_KEY（--key / 环境变量 / .env）", file=sys.stderr)
        return 2

    print(f"评估 {len(questions)} 题 · {args.api} · judge=offline（确定性口径，非 RAGAS 库）")
    rows: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.api, headers={"X-API-Key": key},
                      timeout=args.timeout, trust_env=False) as client:
        for i, spec in enumerate(questions, start=1):
            got = ask(client, spec["question"], timeout=args.timeout)
            row = score_one(spec, got)
            rows.append(row)
            rec = "—" if row["context_recall"] is None else f"{row['context_recall']:.2f}"
            anc = "—" if row["anchor_hit_rate"] is None else f"{row['anchor_hit_rate']:.2f}"
            print(f"  [{i:02d}/{len(questions)}] q{row['id']:<3} "
                  f"recall={rec} anchor={anc} "
                  f"{'DEGRADED ' if row['degraded'] else ''}"
                  f"{'ERROR ' + row['error'] if row['error'] else ''}"
                  f"{row['total_ms'] or 0:.0f}ms")

    report = summarize(rows)
    report.update({
        "mode": "http",
        "judge": "offline-deterministic",
        "note": "确定性口径：context_* 为文档级；anchor_hit_rate 不等于 RAGAS faithfulness",
        "api": args.api,
        "limited_sample": bool(args.limit and args.limit < len(questions)),
        "questions_file": str(args.questions),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": rows,
    })

    out = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"answers_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 汇总（确定性口径，不可与 RAGAS 榜单数字互比）=====")
    for name in ("context_recall", "context_precision", "anchor_hit_rate"):
        value = report.get(name)
        n = report.get(f"{name}_n")
        print(f"  {name:<18} {'—' if value is None else f'{value:.4f}'}  (n={n})")
    print(f"  answers            {report['answers']}")
    print(f"  degraded_rate      {report['degraded_rate']}")
    print(f"  abstention_ok_rate {report['abstention_ok_rate']}  (n={report['unanswerable_n']} 无 golden 题)")
    print(f"  ttft_ms_p50        {report.get('ttft_ms_p50')}")
    print(f"  total_ms_p50/p95   {report.get('total_ms_p50')} / {report.get('total_ms_p95')}")
    if report["limited_sample"]:
        print("  ⚠ limited_sample=true —— 抽样结果，不能当全量汇报")
    print(f"\n产物：{out}")

    if args.publish:
        # 抽样结果**不写大盘**：面板上只允许存在一个口径的数。
        # 让 8 题的 0.9 覆盖 48 题的 0.8785，比面板空着更糟 —— 空着至少是诚实的。
        if report["limited_sample"]:
            print("  [SKIP] limited_sample=true，不写大盘（要写就跑全量，去掉 --limit）")
        else:
            publish_report(report, api=args.api, key=key, top_k=args.top_k)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="答案侧评估（RAGAS 口径的确定性实现）")
    parser.add_argument("--api", default=os.environ.get("EVAL_API", DEFAULT_API))
    parser.add_argument("--key", default=None)
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（抽样，产物会标 limited_sample）")
    parser.add_argument("--only-intent", default="", help="逗号分隔，如 rag,hybrid")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--rescore", default="", help="对已保存的评估产物重打分（不调用模型）")
    parser.add_argument("--publish", action="store_true",
                        help="把汇总写进大盘（POST /admin/eval/metrics）；抽样结果会被跳过")
    parser.add_argument("--top-k", type=int, default=5, help="召回的 top_k，仅用于指标标签")
    args = parser.parse_args(argv)
    if args.rescore:
        return rescore(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
