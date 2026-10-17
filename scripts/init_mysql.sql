-- 模拟"外部业务系统"，给 Java MCP Server 提供真实的数据源。
-- 设计原则：结构化数据（工单状态、资产台账、用户信息）**不应该进向量库**。
-- 把它们做成 MCP 工具让 Agent 实时查询，既是正确做法，也顺便演示了
-- "什么时候该用 RAG、什么时候该用工具调用"这个高频面试题。

CREATE TABLE IF NOT EXISTS users (
    id              BIGINT PRIMARY KEY AUTO_INCREMENT,
    employee_no     VARCHAR(32)  NOT NULL UNIQUE,
    name            VARCHAR(64)  NOT NULL,
    department      VARCHAR(64),
    email           VARCHAR(128),
    phone           VARCHAR(32),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS assets (
    id              BIGINT PRIMARY KEY AUTO_INCREMENT,
    asset_code      VARCHAR(64)  NOT NULL UNIQUE,
    hostname        VARCHAR(128),
    ip              VARCHAR(64),
    category        VARCHAR(32)  COMMENT 'server/db/middleware/vpn/network',
    owner_no        VARCHAR(32),
    location        VARCHAR(64),
    status          VARCHAR(16)  DEFAULT 'online' COMMENT 'online/offline/maintenance',
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS tickets (
    id              BIGINT PRIMARY KEY AUTO_INCREMENT,
    ticket_no       VARCHAR(32)  NOT NULL UNIQUE,
    title           VARCHAR(255) NOT NULL,
    description     TEXT,
    category        VARCHAR(32)  COMMENT 'vpn/database/network/account/other',
    priority        VARCHAR(16)  COMMENT 'P0/P1/P2/P3',
    status          VARCHAR(16)  COMMENT 'open/in_progress/resolved/closed',
    reporter_no     VARCHAR(32),
    assignee_no     VARCHAR(32),
    asset_code      VARCHAR(64),
    resolution      TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at     DATETIME NULL,
    INDEX idx_status (status),
    INDEX idx_reporter (reporter_no),
    INDEX idx_category (category)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ------------------------------ 种子数据 ------------------------------
INSERT INTO users (employee_no, name, department, email) VALUES
  ('E1001', '张伟', '技术部',   'zhangwei@example.com'),
  ('E1002', '李娜', '财务部',   'lina@example.com'),
  ('E1003', '王强', '运维部',   'wangqiang@example.com'),
  ('E1004', '刘洋', '市场部',   'liuyang@example.com');

INSERT INTO assets (asset_code, hostname, ip, category, owner_no, location, status) VALUES
  ('SRV-0001', 'app-prod-01',  '10.0.1.11', 'server',     'E1003', '机房A-机柜3', 'online'),
  ('SRV-0002', 'app-prod-02',  '10.0.1.12', 'server',     'E1003', '机房A-机柜3', 'online'),
  ('DB-0001',  'mysql-prod-01','10.0.2.21', 'db',         'E1003', '机房A-机柜5', 'online'),
  ('VPN-0001', 'vpn-gw-01',    '10.0.9.1',  'vpn',        'E1003', '机房B-机柜1', 'maintenance'),
  ('MW-0001',  'redis-prod-01','10.0.3.31', 'middleware', 'E1003', '机房A-机柜7', 'online');

INSERT INTO tickets (ticket_no, title, description, category, priority, status, reporter_no, assignee_no, asset_code, resolution, resolved_at) VALUES
  ('TK-20260101', 'VPN 连接超时无法访问内网', '在家办公时 VPN 拨号成功但访问不了内部系统', 'vpn', 'P1', 'resolved', 'E1002', 'E1003', 'VPN-0001',
   'VPN 网关证书已过期，更新证书后恢复正常；后续已加入证书到期前 30 天自动告警。', '2026-01-02 10:20:00'),
  ('TK-20260115', '生产数据库连接数打满', '应用报 Too many connections，服务大量超时', 'database', 'P0', 'resolved', 'E1003', 'E1003', 'DB-0001',
   '连接池配置未随压测调整，max_connections 从 200 提到 800，并把应用侧连接池上限下调到 150 避免抢占。', '2026-01-15 16:45:00'),
  ('TK-20260203', '新员工账号未开通邮件权限', '入职三天仍无法登录企业邮箱', 'account', 'P3', 'resolved', 'E1004', 'E1003', NULL,
   '入职流程漏发权限申请单，已补开；建议 HR 入职清单增加权限核对项。', '2026-02-04 09:30:00'),
  ('TK-20260220', '机房 B 网络抖动', '机房 B 到机房 A 的链路每隔几分钟丢包一次', 'network', 'P1', 'in_progress', 'E1003', 'E1003', NULL, NULL, NULL),
  ('TK-20260301', '财务系统导出报表超时', '导出年度报表时页面一直转圈最后报 504', 'other', 'P2', 'open', 'E1002', 'E1003', NULL, NULL, NULL);
