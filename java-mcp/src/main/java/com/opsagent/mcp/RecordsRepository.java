package com.opsagent.mcp;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Component;

import java.io.InputStream;
import java.util.List;
import java.util.Map;

/**
 * 台账数据（工单 / 资产 / 告警）。
 *
 * <p>数据源是打包进 jar 的 records.json —— 这是**演示用的只读快照**，
 * 对应真实系统里"资产台账 DB / 工单系统 API"的位置。把它放在这里而不是
 * 让 Java 服务也去读 mock：MCP Server 应当是一个**自洽的独立服务**，
 * 它的可用性不该依赖 Python 侧的目录挂载。
 *
 * <p>数据量只有 6/3/4 条，所以全量载入内存、线性扫描。真实规模下这里的
 * 每个查询都要走索引或 SQL —— 但那是 D3 的事，现在把契约先固定下来。
 */
@Component
public class RecordsRepository {

    private static final Logger log = LoggerFactory.getLogger(RecordsRepository.class);

    private final ObjectMapper mapper = new ObjectMapper();

    private List<Map<String, Object>> tickets = List.of();
    private List<Map<String, Object>> assets = List.of();
    private List<Map<String, Object>> alerts = List.of();

    @PostConstruct
    void load() throws Exception {
        try (InputStream in = new ClassPathResource("records.json").getInputStream()) {
            Map<String, List<Map<String, Object>>> raw =
                    mapper.readValue(in, new TypeReference<>() {});
            tickets = raw.getOrDefault("tickets", List.of());
            assets = raw.getOrDefault("assets", List.of());
            alerts = raw.getOrDefault("alerts", List.of());
        }
        log.info("records loaded: tickets={}, assets={}, alerts={}",
                tickets.size(), assets.size(), alerts.size());
    }

    public List<Map<String, Object>> tickets() {
        return tickets;
    }

    public List<Map<String, Object>> assets() {
        return assets;
    }

    public List<Map<String, Object>> alerts() {
        return alerts;
    }

    /** 大小写不敏感的比较：工单号/资产码在人工输入里大小写很随意。 */
    static boolean eqIgnoreCase(Object value, String expected) {
        return value != null && String.valueOf(value).equalsIgnoreCase(expected);
    }
}
