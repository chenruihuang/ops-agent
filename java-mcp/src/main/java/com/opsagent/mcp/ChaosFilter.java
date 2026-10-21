package com.opsagent.mcp;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 故障注入（chaos）——**只在请求头触发，默认不生效**。
 *
 * <p>为什么一个"能跑的演示"必须带这个：熔断、重试、降级这三件事，
 * 讲一百遍不如现场演示一次。而要演示就得能**可控地**制造故障，
 * 而不是靠拔网线。
 *
 * <p>用法（客户端在 {@code MCP_CHAOS} 里配，见 McpProvider）：
 * <pre>
 *   X-Chaos: sleep=1200          # 每次调用先卡 1.2s（触发客户端 2s 超时 / 重试）
 *   X-Chaos: error=500           # 恒定 500（触发重试 → 用尽 → 熔断）
 *   X-Chaos: error=500,times=2   # 前 2 次 500，之后正常（验证"重试成功"）
 *   X-Chaos: hang=60000          # 卡 60s（配合客户端 2s 超时）
 * </pre>
 *
 * <p>安全边界：只作用于 {@code /mcp}，不作用于 {@code /health} ——
 * 否则注入故障会连 healthcheck 一起打挂，容器被编排系统反复重启，
 * 演示直接变成事故。
 */
@Component
public class ChaosFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(ChaosFilter.class);

    /** 计数窗口：同一个 chaos 字符串超过这个时长没被触发就重置。
     *
     * <p>不加这个会有一个很坑的体验：演示过一次 {@code error=500,times=2} 之后，
     * 计数停在 2，第二次演示时这个 chaos **立刻失效**（看起来像"注入坏了"）。
     * 计数器按 header 字符串做 key 是刻意的 —— 换一个值就是一组新的注入。
     */
    private static final long WINDOW_MS = 10 * 60 * 1000;

    private final ConcurrentHashMap<String, int[]> counters = new ConcurrentHashMap<>();
    private final MeterRegistry meters;

    public ChaosFilter(MeterRegistry meters) {
        this.meters = meters;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        String chaos = request.getHeader("X-Chaos");
        if (chaos == null || chaos.isBlank() || !request.getRequestURI().startsWith("/mcp")) {
            chain.doFilter(request, response);
            return;
        }

        ChaosSpec spec = ChaosSpec.parse(chaos);
        if (spec.limited() && spent(chaos) >= spec.times) {
            chain.doFilter(request, response);   // 次数用完 → 恢复正常
            return;
        }

        log.info("chaos injected: {} (remaining={})", chaos, spec.describeRemaining());
        // 注入次数必须可见：做故障实验时"到底注入了没有、注入了几次"
        // 不能只靠日志 grep —— 大盘上要能和请求量对得上。
        meters.counter("mcp_chaos_total",
                "mode", spec.errorStatus > 0 ? "error" : "sleep").increment();

        if (spec.sleepMs > 0) {
            try {
                Thread.sleep(spec.sleepMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        if (spec.errorStatus > 0) {
            response.setStatus(spec.errorStatus);
            response.setContentType("text/plain;charset=UTF-8");
            response.getWriter().write("chaos injected: " + spec.errorStatus);
            return;
        }
        chain.doFilter(request, response);
    }

    /** 本次是第几次触发（0 基），同时负责窗口过期重置。 */
    private int spent(String chaos) {
        long now = System.currentTimeMillis();
        int[] slot = counters.computeIfAbsent(chaos, k -> new int[]{0, (int) (now / 1000)});
        synchronized (slot) {
            long firstAt = slot[1] * 1000L;
            if (now - firstAt > WINDOW_MS) {
                slot[0] = 0;
                slot[1] = (int) (now / 1000);
            }
            return slot[0]++;
        }
    }

    /** 解析 {@code sleep=1200} / {@code error=500,times=2} / {@code hang=60000}。 */
    record ChaosSpec(long sleepMs, int errorStatus, int times) {

        static ChaosSpec parse(String raw) {
            long sleep = 0;
            int error = 0;
            int times = -1;
            for (String part : raw.split(",")) {
                String[] kv = part.trim().split("=", 2);
                String key = kv[0].trim().toLowerCase();
                String value = kv.length > 1 ? kv[1].trim() : "";
                try {
                    switch (key) {
                        case "sleep" -> sleep = Long.parseLong(value);
                        case "hang" -> sleep = Long.parseLong(value);
                        case "error" -> error = Integer.parseInt(value);
                        case "times" -> times = Integer.parseInt(value);
                        default -> { }
                    }
                } catch (NumberFormatException ignored) {
                    // 值写错就当没注入：故障开关本身不该引发故障
                }
            }
            return new ChaosSpec(sleep, error, times);
        }

        boolean limited() {
            return times >= 0;
        }

        String describeRemaining() {
            return limited() ? "times=" + times : "unlimited";
        }
    }
}
