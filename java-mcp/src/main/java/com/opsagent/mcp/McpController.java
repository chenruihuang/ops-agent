package com.opsagent.mcp;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * MCP 的 JSON-RPC 入口：{@code POST /mcp}。
 *
 * <p>支持的方法：
 * <ul>
 *   <li>{@code initialize} —— 握手（返回 protocolVersion 与 capabilities）</li>
 *   <li>{@code tools/list} —— 工具清单</li>
 *   <li>{@code tools/call} —— 调用工具</li>
 *   <li>{@code ping} —— 探活</li>
 * </ul>
 *
 * <p><b>错误分两类，不能混</b>：
 * <ul>
 *   <li>JSON-RPC 层的 {@code error}（方法不存在 -32601、参数不是对象 -32602）——
 *       协议级错误，客户端应视为"不可重试"；</li>
 *   <li>工具返回的 {@code isError: true} —— 业务级失败（比如工单号格式对但查不到），
 *       HTTP 仍然是 200、JSON-RPC 仍然是 result。客户端会降级，但**不计入熔断**。</li>
 * </ul>
 * 如果把业务失败也做成 5xx，客户端的熔断器会把"用户输错单号"统计成"依赖不健康"，
 * 5 次就能把服务摘掉 —— 这是很典型的误伤。
 */
@RestController
public class McpController {

    private static final Logger log = LoggerFactory.getLogger(McpController.class);
    private static final String PROTOCOL_VERSION = "2024-11-05";

    private final ToolCatalog catalog;
    private final ObjectMapper mapper = new ObjectMapper();
    private final MeterRegistry meters;

    public McpController(ToolCatalog catalog, MeterRegistry meters) {
        this.catalog = catalog;
        this.meters = meters;
    }

    /** 供 compose healthcheck 与 /ready 探活。故意不暴露工具明细（避免信息面过大）。 */
    @GetMapping(value = "/health", produces = MediaType.APPLICATION_JSON_VALUE)
    public Map<String, Object> health() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", "ok");
        out.put("tools", catalog.size());
        return out;
    }

    @PostMapping(value = "/mcp", produces = MediaType.APPLICATION_JSON_VALUE)
    public Map<String, Object> rpc(@RequestBody Map<String, Object> body) {
        long started = System.currentTimeMillis();
        Object id = body.get("id");
        String method = body.get("method") == null ? "" : String.valueOf(body.get("method"));
        Object paramsRaw = body.get("params");
        Map<String, Object> params = asMap(paramsRaw);

        try {
            Object result = switch (method) {
                case "initialize" -> initialize();
                case "ping" -> Map.of();
                case "tools/list" -> Map.of("tools", catalog.list());
                case "tools/call" -> callTool(params);
                default -> throw new RpcError(-32601, "Method not found: " + method);
            };
            long ms = System.currentTimeMillis() - started;
            meters.counter("mcp_rpc_total", "method", method, "outcome", "ok").increment();
            log.info("rpc method={} name={} ok=true ms={}", method,
                    String.valueOf(params.getOrDefault("name", "-")), ms);
            return envelope(id, "result", result);
        } catch (RpcError e) {
            meters.counter("mcp_rpc_total", "method", method, "outcome", "rpc_error").increment();
            log.warn("rpc method={} code={} msg={}", method, e.code, e.getMessage());
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("code", e.code);
            err.put("message", e.getMessage());
            return envelope(id, "error", err);
        } catch (Exception e) {
            // 未预期异常：JSON-RPC 的 internal error。客户端会当可重试错误处理。
            meters.counter("mcp_rpc_total", "method", method, "outcome", "internal_error").increment();
            log.error("rpc method={} internal error", method, e);
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("code", -32603);
            err.put("message", "Internal error: " + e.getClass().getSimpleName());
            return envelope(id, "error", err);
        }
    }

    // ---------------- 内部 ----------------
    private Object initialize() {
        Map<String, Object> serverInfo = new LinkedHashMap<>();
        serverInfo.put("name", "ops-agent-java-mcp");
        serverInfo.put("version", "1.0.0");

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("protocolVersion", PROTOCOL_VERSION);
        out.put("capabilities", Map.of("tools", Map.of("listChanged", false)));
        out.put("serverInfo", serverInfo);
        return out;
    }

    private Object callTool(Map<String, Object> params) {
        String name = String.valueOf(params.getOrDefault("name", ""));
        if (name.isBlank()) {
            throw new RpcError(-32602, "tools/call 缺少 name");
        }
        if (!catalog.has(name)) {
            throw new RpcError(-32602, "未知工具：" + name);
        }
        Map<String, Object> args = asMap(params.get("arguments"));
        // 每个工具一个 Timer：只报平均耗时会让 P95 无从谈起，
        // 而"工单查询偶发 2s"这种问题正是要靠分位定位的。
        Timer.Sample sample = Timer.start(meters);
        try {
            Object result = catalog.call(name, args);
            meters.counter("mcp_tool_calls_total", "tool", name, "result", "ok").increment();
            return result;
        } catch (RuntimeException e) {
            meters.counter("mcp_tool_calls_total", "tool", name, "result", "error").increment();
            throw e;
        } finally {
            sample.stop(meters.timer("mcp_tool_seconds", "tool", name));
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object raw) {
        if (raw instanceof Map<?, ?> map) {
            return (Map<String, Object>) map;
        }
        return Map.of();
    }

    private static Map<String, Object> envelope(Object id, String key, Object value) {
        // 用 LinkedHashMap 而不是 Map.of：id 可能是 null（通知类请求），Map.of 不接受 null
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("jsonrpc", "2.0");
        out.put("id", id);
        out.put(key, value);
        return out;
    }

    /** JSON-RPC 协议错误。与工具的业务失败（isError）是两回事，见类注释。 */
    static class RpcError extends RuntimeException {
        final int code;

        RpcError(int code, String message) {
            super(message);
            this.code = code;
        }
    }
}
