# 审计日志空间需求评估报告

## 📊 实际测量数据

### 当前单次运行的磁盘占用

基于实际运行 `artifacts/daily_alpaca_scheduler/output/20260627_120000/` 的测量：

| 文件 | 大小 | 占比 | 说明 |
|------|------|------|------|
| `alpha_core_panel_20260627.csv` | 698 KB | 75% | 918 只股票 × 10+ 因子 |
| `lot_snapshot_20260627.json` | 122 KB | 13% | Lot ledger 快照 |
| `broker_positions_before.csv` | 32 KB | 3.4% | 执行前持仓 |
| `broker_positions_after.csv` | 32 KB | 3.4% | 执行后持仓 |
| `industry_map_dynamic.csv` | 18 KB | 2% | SIC → 行业映射 |
| `order_plan.json` | 7 KB | 0.8% | 订单计划 |
| `decision_targets.csv` | 3 KB | 0.3% | 目标权重 |
| `execution_summary.json` | 2 KB | 0.2% | 执行总结 |
| `execution_records.json` | 2 bytes | 0% | 当前为空（plan_only 模式） |
| **总计** | **929 KB** | **100%** | |

---

## 🔧 方案 A 增强后的空间预算

### 新增文件

| 文件 | 预估大小 | 说明 |
|------|---------|------|
| `optimizer_diagnostics.json` | 1 KB | 优化器求解状态、约束、失败提示 |
| `universe_filtering.json` | 0.5 KB | 股票池过滤统计 |
| `execution_records.json` (增强) | 15 KB | 60 笔订单 × ~250 bytes（含错误信息） |

### 单次运行总计

```
当前：929 KB
增强：929 + 1 + 0.5 + 14 = 944.5 KB ≈ 950 KB
增量：21 KB (+2.3%)
```

---

## 📅 时间维度的空间需求

### 每日需求

**运行频率**：每天 2 次
- 12:00 北京时间：决策（DecisionEngine）
- 22:00 北京时间：执行（Alpaca Executor）

| 方案 | 每日空间 | 计算 |
|------|---------|------|
| 当前 | **1.81 MB/天** | 929 KB × 2 |
| 方案 A | **1.86 MB/天** | 950 KB × 2 |
| 增量 | **0.05 MB/天** | 21 KB × 2 |

### 每周需求（5 个交易日）

| 方案 | 每周空间 |
|------|---------|
| 当前 | **9.1 MB/周** |
| 方案 A | **9.3 MB/周** |
| 增量 | **0.2 MB/周** |

### 每月需求（21 个交易日）

| 方案 | 每月空间 |
|------|---------|
| 当前 | **38.1 MB/月** |
| 方案 A | **39.0 MB/月** |
| 增量 | **0.9 MB/月** |

### 每年需求（252 个交易日）

| 方案 | 每年空间 |
|------|---------|
| 当前 | **457 MB/年 (0.45 GB)** |
| 方案 A | **468 MB/年 (0.46 GB)** |
| 增量 | **11 MB/年** |

### 5 年累积

| 方案 | 5 年总计 |
|------|---------|
| 当前 | **2.25 GB** |
| 方案 A | **2.30 GB** |
| 增量 | **55 MB** |

---

## 🗜️ 压缩存储策略

### 压缩比实测

对实际文件的 gzip 压缩测试：

| 文件类型 | 原始大小 | 压缩后 | 压缩比 |
|---------|---------|--------|--------|
| `alpha_core_panel.csv` | 698 KB | 226 KB | **68%** |
| `lot_snapshot.json` | 122 KB | 8.1 KB | **94%** |
| 平均（加权） | 929 KB | ~280 KB | **70%** |

### 压缩后的空间需求

| 周期 | 无压缩 | 压缩后 (70%) | 节省 |
|------|--------|-------------|------|
| 每日 | 1.86 MB | **0.56 MB** | 1.3 MB |
| 每月 | 39.0 MB | **11.7 MB** | 27.3 MB |
| 每年 | 468 MB | **140 MB** | 328 MB |
| 5 年 | 2.30 GB | **0.69 GB** | 1.61 GB |

### 建议的压缩策略

#### 方案 1：全量即时压缩

```bash
# 每次运行后自动压缩整个目录
cd artifacts/daily_alpaca_scheduler/output/
tar -czf 20260627_120000.tar.gz 20260627_120000/
rm -rf 20260627_120000/
```

**优点**：最省空间（70% 压缩比）
**缺点**：需要解压才能查看日志

#### 方案 2：保留近期，压缩历史

```bash
# 保留最近 30 天原始，压缩 30 天以前的
find artifacts/daily_alpaca_scheduler/output/ \
  -maxdepth 1 -type d -mtime +30 \
  -exec tar -czf {}.tar.gz {} \; \
  -exec rm -rf {} \;
```

**优点**：最近日志快速访问，历史压缩节省空间
**缺点**：需要自动化脚本

#### 方案 3：压缩大文件，保留小文件

```bash
# 只压缩 alpha_core_panel 和 lot_snapshot（占 88%）
cd 20260627_120000/
gzip -9 alpha_core_panel_20260627.csv
gzip -9 lot_snapshot_20260627.json
# 其他小文件保持原样便于查看
```

**优点**：节省大部分空间（~80%），小文件仍可直接查看
**缺点**：需要 gunzip 才能看因子面板

**推荐**：方案 2（保留近期，压缩历史）

---

## 🧹 日志清理策略

### 策略 1：按时间滚动删除

| 保留期 | 空间占用 (无压缩) | 空间占用 (压缩) |
|--------|------------------|----------------|
| 最近 1 个月 | 39 MB | 12 MB |
| 最近 3 个月 | 117 MB | 35 MB |
| 最近 6 个月 | 234 MB | 70 MB |
| 最近 1 年 | 468 MB | 140 MB |
| 永久保留 | ∞ | ∞ |

**建议**：保留最近 1 年原始日志（468 MB），1 年以前压缩归档或删除。

### 策略 2：仅保留异常日志

定期扫描并清理"正常运行"的日志，保留：
- 优化器失败的会话
- 订单提交失败的会话
- 执行滑点异常的会话
- 账户权益异常波动的会话

**估算**：假设 95% 运行正常，清理后空间需求降至 **5%**（~23 MB/年）

### 策略 3：分层存储

| 日志类型 | 保留期 | 存储方式 |
|---------|--------|---------|
| 最近 30 天 | 原始 | 快速 SSD |
| 31-365 天 | 压缩 | 普通 SSD |
| 1-3 年 | 压缩 | 机械硬盘或云存储 |
| 3 年以上 | 归档或删除 | S3/Glacier（可选） |

---

## 💾 当前磁盘状态

### W: 盘空间

```
Filesystem: W:
Total: 884 GB
Used: 841 GB
Available: 44 GB
Usage: 96%
```

**结论**：当前 W 盘已接近满载（96%），但审计日志即使 5 年累积也仅 2.3 GB，**不是主要空间压力来源**。

### 当前项目空间占用

```
us-quant-live 项目总计: 3.6 GB
  - venv: ~2.8 GB (pandas/numpy/scipy 依赖)
  - data: ~0.5 GB (历史市场数据缓存)
  - artifacts: ~0.3 GB (包括审计日志)
```

---

## 🎯 空间规划建议

### 短期（接下来 3 个月）

**无需提前腾空间**

- 审计日志增长：3 个月 × 39 MB = **117 MB**
- 当前可用空间：**44 GB**
- 占用比例：0.26%

### 中期（接下来 1 年）

**建议预留 500 MB**

- 审计日志 1 年增长：**468 MB**
- 额外 buffer（临时文件、数据缓存）：32 MB
- 总计：**500 MB**

**操作**：可以从当前 44 GB 可用空间中分配，无需专门腾空间。

### 长期（3-5 年）

**建议实施压缩+清理策略**

不实施任何清理的最坏情况：
- 5 年审计日志累积：**2.3 GB**
- 5 年 venv/data 增长：**~1 GB**
- 总计：**~3.3 GB**

实施"保留 1 年 + 压缩历史"策略后：
- 最近 1 年原始日志：468 MB
- 历史 4 年压缩日志：4 × 140 MB = 560 MB
- 总计：**~1 GB**（节省 **2.3 GB**）

---

## 📋 自动化清理脚本（建议实施）

### 脚本 1：每周压缩 30 天前的日志

```bash
#!/bin/bash
# 文件：tools/compress_old_logs.sh

LOG_ROOT="artifacts/daily_alpaca_scheduler/output"
CUTOFF_DAYS=30

find "$LOG_ROOT" -maxdepth 1 -type d -mtime +$CUTOFF_DAYS | while read dir; do
    if [[ -d "$dir" && ! -f "${dir}.tar.gz" ]]; then
        echo "Compressing $dir..."
        tar -czf "${dir}.tar.gz" -C "$(dirname "$dir")" "$(basename "$dir")"
        rm -rf "$dir"
        echo "  → ${dir}.tar.gz (saved $(du -h "${dir}.tar.gz" | cut -f1))"
    fi
done
```

**定时运行**：每周日凌晨 3:00
```bash
# Windows Task Scheduler 或 cron
0 3 * * 0 bash /w/Quat/us-quant-live/tools/compress_old_logs.sh
```

### 脚本 2：每月清理 1 年前的日志

```bash
#!/bin/bash
# 文件：tools/cleanup_old_logs.sh

LOG_ROOT="artifacts/daily_alpaca_scheduler/output"
CUTOFF_DAYS=365

echo "=== Deleting logs older than ${CUTOFF_DAYS} days ==="
find "$LOG_ROOT" -maxdepth 1 \( -type d -o -name "*.tar.gz" \) -mtime +$CUTOFF_DAYS | while read path; do
    echo "Deleting: $path"
    rm -rf "$path"
done

echo "=== Current disk usage ==="
du -sh "$LOG_ROOT"
```

**定时运行**：每月 1 号凌晨 2:00

---

## 📊 总结表

| 维度 | 当前 | 方案 A | 方案 A + 压缩 |
|------|------|--------|--------------|
| **单次运行** | 929 KB | 950 KB | 285 KB |
| **每日** | 1.81 MB | 1.86 MB | 0.56 MB |
| **每月** | 38.1 MB | 39.0 MB | 11.7 MB |
| **每年** | 457 MB | 468 MB | 140 MB |
| **5 年** | 2.25 GB | 2.30 GB | 0.69 GB |

### 关键结论

1. ✅ **审计日志增强的空间成本极低**：方案 A 每年仅增加 11 MB（+2.3%）
2. ✅ **不需要提前腾空间**：1 年累积仅 468 MB，当前 44 GB 可用空间绰绰有余
3. ✅ **压缩效果显著**：gzip 可节省 70% 空间，5 年从 2.3 GB 降至 0.69 GB
4. ✅ **W 盘 96% 占用的主要来源不是审计日志**：venv (2.8 GB) 和历史数据 (0.5 GB) 才是大头
5. ⚠️ **建议实施自动化清理**：每周压缩 30 天前日志，每月删除 1 年前日志，长期稳定在 ~500 MB

### 立即行动项

**现在无需任何操作**，但建议在接下来 1-2 周内：

1. 创建 `tools/compress_old_logs.sh`（每周压缩脚本）
2. 创建 `tools/cleanup_old_logs.sh`（每月清理脚本）
3. 配置 Windows Task Scheduler 定时运行
4. 可选：添加日志查看工具 `tools/view_archived_log.sh`（自动解压 + 查看 + 重新压缩）

---

**报告生成时间**：2026-06-27
**测量基准**：实际运行 `20260627_120000` 目录
**磁盘状态**：W: 盘 44 GB 可用 / 884 GB 总计（96% 占用）
