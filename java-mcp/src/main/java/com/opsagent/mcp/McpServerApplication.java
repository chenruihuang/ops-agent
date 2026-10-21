package com.opsagent.mcp;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * MCP 工具服务的入口。
 *
 * <p>为什么是 Spring Boot + JSON-RPC over HTTP，而不是官方 MCP SDK 的 stdio：
 * 客户端（Python）跑在容器里，stdio 需要额外的进程管理与管道生命周期，
 * 而 HTTP 端点天然可 healthcheck、可限流、可抓包。协议层完全遵循 MCP 的
 * JSON-RPC 形状（initialize / tools/list / tools/call），换 transport 只动这里。
 */
@SpringBootApplication
public class McpServerApplication {

    public static void main(String[] args) {
        SpringApplication.run(McpServerApplication.class, args);
    }
}
