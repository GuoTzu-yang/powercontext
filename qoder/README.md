# StudyFlow · 科研与学习效率工作台

一个用 Qoder 做出来的单页效率工具：**番茄专注** + **论文速读卡片** + **JSON / Markdown 导出**。
卡片数据落在 **seekdb**（MySQL 协议兼容的数据库）里，换设备也能取回。

## 组成

| 文件 | 作用 |
| --- | --- |
| `dist/index.html` / `index.html` | 单文件前端：零依赖、零构建，直接打开就能用 |
| `studyflow_seekdb.py` | seekdb 存储层：建库建表、卡片增查、专注会话统计（`pymysql`） |
| `serve.py` | 本地服务：静态站点 + `/api/health`、`/api/cards`、`/api/focus`（仅标准库） |

## 跑起来

### 1. 只用前端（不需要数据库）

双击 `index.html`。所有卡片存在浏览器 `localStorage`，导出按钮直接产出 JSON / Markdown。

### 2. 接上 seekdb（卡片持久化到数据库）

```bash
# seekdb 实例（任意兼容 MySQL 协议的端点都可以）
export STUDYFLOW_DB_HOST=127.0.0.1
export STUDYFLOW_DB_PORT=2881     # 本地 seekdb 实例端口
export STUDYFLOW_DB_USER=root
export STUDYFLOW_DB_PASSWORD=

pip install pymysql
python serve.py 8765              # 自动建库建表，然后打开 http://127.0.0.1:8765/
```

页面底部会出现 **seekdb 同步** 面板：

- 绿标「seekdb 已连接 host:port」表示 `study_cards` 表可读写；
- 「同步本地卡片到 seekdb」把 localStorage 里的卡片逐条 `INSERT`；
- 面板里列出库中最近的卡片标题。

未启动 `serve.py` 时面板显示「本地模式」，功能完全不受影响。

## 数据模型

```sql
CREATE TABLE study_cards (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  background TEXT, method TEXT, conclusion TEXT, limitation TEXT,
  created_at BIGINT NOT NULL
);

CREATE TABLE focus_sessions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  started_at BIGINT NOT NULL, minutes INT NOT NULL, label VARCHAR(255)
);
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 连通性与卡片总数（页面徽标用） |
| GET | `/api/cards` | 最近 100 张卡片 |
| POST | `/api/cards` | 新增卡片 `{title, background, method, conclusion, limitation}` |
| GET | `/api/focus` | 累计专注分钟数 |
| POST | `/api/focus` | 记录一次专注 `{minutes, label}` |

## 设计取舍

- **前端零依赖**：单文件 HTML，不引入框架，离线可用；`localStorage` 是默认路径，seekdb 是可选增强。
- **数据库可选**：`serve.py` 起不来时页面自动降级，不做硬依赖——效率工具不应该因为数据库没开就不能用。
- **只用标准库的服务端**：`http.server` + `pymysql`，没有 Web 框架，部署成本接近零。
