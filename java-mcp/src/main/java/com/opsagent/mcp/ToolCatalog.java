package com.opsagent.mcp;

import jakarta.annotation.PostConstruct;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 工具目录：5 个工具的声明与实现。
 *
 * <p><b>契约来源</b>：与 Python 侧 {@code app/tools/providers.py:TOOL_SPECS} 严格同名、
 * 同参数、同返回形状。两边不是"靠自觉保持一致"——集成测试里有一条用例把
 * {@code tools/list} 的结果与 TOOL_SPECS 逐项比对（名字、required 参数），
 * 漂移会直接让测试变红。
 *
 * <p><b>返回形状</b>（MCP 规定）：
 * <pre>
 *   {"content": [{"type": "text", "text": "给人/模型看的文本"}],
 *    "structuredContent": {"items": [...], "count": n},
 *    "isError": false}
 * </pre>
 * 文本服务模型，items 服务代码（下游要算数、要引用字段时不用再解析中文）。
 *
 * <p><b>为什么强调"查不到要说清楚"</b>：空字符串会让模型分不清"没查到"与
 * "本来就没有"，于是拿知识库内容硬答实时问题——这是工具类问答最常见的失败。
 * 所以每个查询都必须返回一句明确的"未找到 / 0 条"。
 */
@Component
public class ToolCatalog {

    /** 工具声明。inputSchema 用 JSON Schema 子集 —— 参数都只有 1~2 个字符串。 */
    public record ToolDef(String name, String description, Map<String, Object> inputSchema) {}

    private final RecordsRepository repo;
    private final Map<String, ToolDef> defs = new LinkedHashMap<>();

    public ToolCatalog(RecordsRepository repo) {
        this.repo = repo;
    }

    @PostConstruct
    void init() {
        register("query_ticket_by_no",
                "按工单号精确查询工单的处理进度、责任人与结论",
                Map.of("ticket_no", str("工单号，形如 INC-2026-0412")),
                List.of("ticket_no"));
        register("query_ticket_by_keyword",
                "按关键词检索历史工单（支持现象、服务名、组件名）",
                Map.of("keyword", str("检索关键词"), "top_k", num("返回条数，默认 3")),
                List.of("keyword"));
        register("query_ticket_by_service",
                "查询某个服务名下的全部工单",
                Map.of("service", str("服务名，形如 order-service")),
                List.of("service"));
        register("get_asset_by_code",
                "按资产编码查询服务器的主机名、IP、状态与承载服务",
                Map.of("asset_code", str("资产编码，形如 SRV-0088")),
                List.of("asset_code"));
        register("query_alerts_by_service",
                "查询某个服务当前未关闭（firing）的告警；无告警时明确返回 0 条",
                Map.of("service", str("服务名，形如 order-service")),
                List.of("service"));
    }

    // ---------------- 声明 ----------------
    private void register(String name, String desc, Map<String, Object> props, List<String> required) {
        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", props);
        schema.put("required", required);
        defs.put(name, new ToolDef(name, desc, schema));
    }

    private static Map<String, Object> str(String desc) {
        return Map.of("type", "string", "description", desc);
    }

    private static Map<String, Object> num(String desc) {
        return Map.of("type", "integer", "description", desc);
    }

    public List<Map<String, Object>> list() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (ToolDef d : defs.values()) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("name", d.name());
            item.put("description", d.description());
            item.put("inputSchema", d.inputSchema());
            out.add(item);
        }
        return out;
    }

    public boolean has(String name) {
        return defs.containsKey(name);
    }

    public int size() {
        return defs.size();
    }

    // ---------------- 调用 ----------------
    /**
     * 按名字分派。参数缺失返回 isError=true（**不是** JSON-RPC error）——
     * 这是"业务层面这次问错了"，客户端应当降级但**不该计入熔断**。
     */
    public Map<String, Object> call(String name, Map<String, Object> args) {
        try {
            return switch (name) {
                case "query_ticket_by_no" -> queryTicketByNo(argStr(args, "ticket_no"));
                case "query_ticket_by_keyword" -> queryTicketByKeyword(
                        argStr(args, "keyword"), argInt(args, "top_k", 3));
                case "query_ticket_by_service" -> queryTicketByService(argStr(args, "service"));
                case "get_asset_by_code" -> getAssetByCode(argStr(args, "asset_code"));
                case "query_alerts_by_service" -> queryAlertsByService(argStr(args, "service"));
                default -> error("未知工具：" + name, "tool_not_found");
            };
        } catch (IllegalArgumentException e) {
            return error(e.getMessage(), "bad_request");
        }
    }

    // ---------------- 实现 ----------------
    private Map<String, Object> queryTicketByNo(String ticketNo) {
        requireText(ticketNo, "ticket_no");
        List<Map<String, Object>> hits = repo.tickets().stream()
                .filter(t -> RecordsRepository.eqIgnoreCase(t.get("ticket_no"), ticketNo))
                .toList();
        if (hits.isEmpty()) {
            return ok("工单 " + ticketNo + "：未找到（请确认单号是否正确）", List.of());
        }
        return ok(renderTicket(hits.get(0)), hits);
    }

    private Map<String, Object> queryTicketByKeyword(String keyword, int topK) {
        requireText(keyword, "keyword");
        if (topK <= 0 || topK > 20) {
            topK = 3;
        }
        List<String> terms = tokenize(keyword);
        if (terms.isEmpty()) {
            return ok("按关键词「" + keyword + "」检索工单：未找到匹配的工单", List.of());
        }

        record Hit(Map<String, Object> ticket, int score) {}
        List<Hit> scored = new ArrayList<>();
        for (Map<String, Object> t : repo.tickets()) {
            int score = 0;
            // 字段权重与 Python 侧一致：标题 3 / 摘要 2 / 服务 2 / 负责人 1。
            // 两边权重不同会导致"同一个问题在 MCP 与 mock 下命中不同工单"，
            // 这种差异极难发现，所以权重表必须抄同一份。
            score += matchScore(t.get("title"), terms) * 3;
            score += matchScore(t.get("summary"), terms) * 2;
            score += matchScore(t.get("service"), terms) * 2;
            score += matchScore(t.get("owner"), terms) * 1;
            if (score > 0) {
                scored.add(new Hit(t, score));
            }
        }
        scored.sort(Comparator.comparingInt(Hit::score).reversed());
        List<Map<String, Object>> items = scored.stream()
                .limit(topK).map(Hit::ticket).toList();

        if (items.isEmpty()) {
            return ok("按关键词「" + keyword + "」检索工单：未找到匹配的工单", List.of());
        }
        StringBuilder sb = new StringBuilder("按关键词「" + keyword + "」检索工单：" + items.size() + " 条");
        for (Map<String, Object> t : items) {
            sb.append("\n").append(renderTicket(t));
        }
        return ok(sb.toString(), items);
    }

    private Map<String, Object> queryTicketByService(String service) {
        requireText(service, "service");
        List<Map<String, Object>> items = repo.tickets().stream()
                .filter(t -> RecordsRepository.eqIgnoreCase(t.get("service"), service))
                .toList();
        return ok(renderTickets("服务 " + service + " 的工单", items), items);
    }

    private Map<String, Object> getAssetByCode(String assetCode) {
        requireText(assetCode, "asset_code");
        List<Map<String, Object>> hits = repo.assets().stream()
                .filter(a -> RecordsRepository.eqIgnoreCase(a.get("asset_code"), assetCode))
                .toList();
        if (hits.isEmpty()) {
            return ok("资产 " + assetCode + "：未找到（请确认资产编码是否正确）", List.of());
        }
        return ok(renderAsset(hits.get(0)), hits);
    }

    private Map<String, Object> queryAlertsByService(String service) {
        requireText(service, "service");
        // 只回未关闭的：resolved 混进来会让模型把已恢复的故障说成正在发生
        List<Map<String, Object>> firing = repo.alerts().stream()
                .filter(a -> RecordsRepository.eqIgnoreCase(a.get("service"), service))
                .filter(a -> "firing".equalsIgnoreCase(String.valueOf(a.get("status"))))
                .toList();

        if (firing.isEmpty()) {
            return ok("服务 " + service + " 的未关闭告警：0 条\n"
                    + "  （" + service + " 当前没有未关闭的告警）", List.of());
        }
        StringBuilder sb = new StringBuilder(
                "服务 " + service + " 的未关闭告警：" + firing.size() + " 条");
        for (int i = 0; i < firing.size(); i++) {
            Map<String, Object> a = firing.get(i);
            sb.append("\n  ").append(i + 1).append(". [").append(a.getOrDefault("severity", "-"))
              .append("] ").append(a.getOrDefault("alert_name", "-"))
              .append(" ｜ ").append(a.getOrDefault("started_at", "")).append(" 起 ｜ ")
              .append(a.getOrDefault("summary", ""));
        }
        return ok(sb.toString(), firing);
    }

    // ---------------- 渲染 ----------------
    private static String renderTicket(Map<String, Object> t) {
        StringBuilder meta = new StringBuilder();
        appendIf(meta, "服务：", t.get("service"));
        appendIf(meta, "状态：", t.get("status"));
        appendIf(meta, "责任方：", t.get("owner"));

        StringBuilder sb = new StringBuilder();
        sb.append("工单 ").append(t.getOrDefault("ticket_no", "?"))
          .append(" ｜ ").append(t.getOrDefault("title", ""));
        if (!meta.isEmpty()) {
            sb.append("\n  ").append(meta);
        }
        Object summary = t.get("summary");
        if (summary != null && !String.valueOf(summary).isEmpty()) {
            sb.append("\n  详情：").append(summary);
        }
        Object runbook = t.get("related_runbook");
        if (runbook != null && !String.valueOf(runbook).isEmpty()) {
            sb.append("\n  关联处置手册：").append(runbook);
        }
        return sb.toString();
    }

    private static String renderAsset(Map<String, Object> a) {
        StringBuilder meta = new StringBuilder();
        appendIf(meta, "IP：", a.get("ip"));
        appendIf(meta, "状态：", a.get("status"));
        appendIf(meta, "位置：", a.get("room"));

        StringBuilder sb = new StringBuilder();
        sb.append("资产 ").append(a.getOrDefault("asset_code", "?"))
          .append(" ｜ 主机名 ").append(a.getOrDefault("hostname", ""));
        if (!meta.isEmpty()) {
            sb.append("\n  ").append(meta);
        }
        Object services = a.get("services");
        if (services instanceof List<?> list && !list.isEmpty()) {
            sb.append("\n  承载服务：").append(String.join("、",
                    list.stream().map(String::valueOf).toList()));
        }
        return sb.toString();
    }

    private static String renderTickets(String label, List<Map<String, Object>> items) {
        if (items.isEmpty()) {
            return label + "：未找到匹配的工单";
        }
        StringBuilder sb = new StringBuilder(label + "：" + items.size() + " 条");
        for (Map<String, Object> t : items) {
            sb.append("\n").append(renderTicket(t));
        }
        return sb.toString();
    }

    private static void appendIf(StringBuilder sb, String label, Object value) {
        // 字段缺失时不该出现"状态：null"这种噪音 —— 判断值本身，不去猜拼接结果
        if (value == null || String.valueOf(value).isEmpty()) {
            return;
        }
        if (!sb.isEmpty()) {
            sb.append(" ｜ ");
        }
        sb.append(label).append(value);
    }

    // ---------------- 结果包装 ----------------
    private static Map<String, Object> ok(String text, List<Map<String, Object>> items) {
        Map<String, Object> structured = new LinkedHashMap<>();
        structured.put("items", items);
        structured.put("count", items.size());

        Map<String, Object> block = new LinkedHashMap<>();
        block.put("type", "text");
        block.put("text", text);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("content", List.of(block));
        out.put("structuredContent", structured);
        out.put("isError", false);
        return out;
    }

    private static Map<String, Object> error(String message, String code) {
        Map<String, Object> block = new LinkedHashMap<>();
        block.put("type", "text");
        block.put("text", "[" + code + "] " + message);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("content", List.of(block));
        out.put("isError", true);
        return out;
    }

    // ---------------- 参数 ----------------
    private static void requireText(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("缺少参数 " + name);
        }
    }

    private static String argStr(Map<String, Object> args, String key) {
        Object v = args == null ? null : args.get(key);
        return v == null ? "" : String.valueOf(v).trim();
    }

    private static int argInt(Map<String, Object> args, String key, int dft) {
        Object v = args == null ? null : args.get(key);
        if (v == null) {
            return dft;
        }
        try {
            return Integer.parseInt(String.valueOf(v).trim());
        } catch (NumberFormatException e) {
            // 参数类型不对不该 500：退回默认值，并让调用方从结果里看不出异常
            return dft;
        }
    }

    /** 切词：空格/标点分隔，保留中文串整体与英文数字串。与 Python 侧的
     * "去掉功能词后取内容词"不同 —— 这里只做最朴素的切分，误差由权重兜底。 */
    private static List<String> tokenize(String text) {
        List<String> out = new ArrayList<>();
        for (String raw : text.split("[\\s,，。;；、]+")) {
            String t = raw.trim();
            if (!t.isEmpty() && !out.contains(t)) {
                out.add(t);
            }
        }
        return out;
    }

    private static int matchScore(Object field, List<String> terms) {
        if (field == null) {
            return 0;
        }
        String value = String.valueOf(field).toLowerCase();
        int hits = 0;
        for (String term : terms) {
            if (value.contains(term.toLowerCase())) {
                hits++;
            }
        }
        return hits;
    }
}
