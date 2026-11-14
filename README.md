# PM2.5 逐时数据发布平台

空气监测站管理员将仪器导出的逐时 PM2.5 CSV 发布给分析人员的网页应用。
Flask + SQLite，单容器 Docker Compose 启动。

## 快速开始（Docker）

```bash
docker compose up --build
```

浏览器打开 **http://localhost:5000** ：

- **上传发布**：选站点、选日期、上传 CSV → 预览读数与错误行 → 确认发布；
- **浏览数据**：按站点 + 日期查看当前已发布批次（版本号、发布时间、全部读数）。

数据持久化在名为 `pm25-data` 的卷中（容器内 `/data/pm25.db`）。
停止后 `docker compose down`，数据保留；`docker compose down -v` 会连同数据一起删除。

## CSV 格式

- UTF-8 编码（兼容 BOM），首行为表头；
- 必须包含**采样时间**与**浓度**两列，兼容别名：
  `sampled_at` / `timestamp` / `time` / `datetime` / `时间`，
  `concentration` / `pm25` / `pm2.5` / `value`；
- 采样时间示例：`2026-11-14 08:00`、`2026-11-14T08:00:00`、`2026/11/14 08:00`；
- 多余列被忽略，空行被跳过。

示例文件：`sample_data/valid.csv`（24 条合法逐时数据）、
`sample_data/invalid.csv`（含各类错误行）。

## 校验与发布规则

| 规则 | 行为 |
| --- | --- |
| 采样时间不在所选日期 / 时刻重复 / 浓度为负数 / 浓度非有限数（NaN、Inf）/ 无法解析 | 该行为错误行；**存在任一错误行，整份文件不可发布**（预览页不显示确认按钮，直接 POST 确认也会被拒） |
| 确认发布 | 在**单个 SQLite 事务**内写入新批并切换当前批次指针：浏览页只会看到完整旧批或完整新批 |
| 基于同一旧批的两份预览 | 先确认者生效；后确认者收到 **409 冲突**页，需重新上传预览（乐观并发控制，预览记录 `base_version`，确认时比对当前版本） |
| 重复确认同一预览 | 幂等：直接跳转浏览页，**不会再次换版** |
| 发布过程中失败 | 事务回滚，**旧批原样保留** |

实现要点：确认时执行 `BEGIN IMMEDIATE` 取得写锁，使并发确认串行化；
版本检查、读数写入、批次指针切换、预览状态更新全部在同一事务内完成。
SQLite 开启 WAL，发布写入不阻塞浏览读取。

## 本地开发（不用 Docker）

```bash
python3 -m pip install -r requirements.txt
python3 app.py          # http://localhost:5000
```

环境变量：`PORT`（默认 5000）、`DB_PATH`（默认 `./data/pm25.db`）、`SECRET_KEY`。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

覆盖：合法上传-预览-确认-浏览全流程、各类错误行拦截、整批替换、
同一旧批双预览冲突、重复确认幂等、失败保留旧批、多线程并发确认串行化、
空日首次发布竞争、英文表头与 ISO 时间兼容。

## 目录结构

```
app.py               # 应用（解析校验、路由、事务化发布）
templates/           # 页面模板（上传、预览、冲突、浏览）
static/style.css
sample_data/         # 示例 CSV
tests/test_app.py    # pytest 端到端测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
