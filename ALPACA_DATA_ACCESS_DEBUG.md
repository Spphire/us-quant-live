# Alpaca 数据访问问题排查

## 问题现象
在本机运行 `alpaca_executor.py --feed iex` 报错：
```
HTTP 403: "subscription does not permit querying recent SIP data"
```

但你说在另一台机器上能正常运行。

## 可能的原因

### 1. 另一台机器用的是不同的 Alpaca 账户
**最可能**：另一台机器的配置文件里用的是：
- Live trading account（即使余额很小，数据权限也更好）
- 或者已经升级过数据订阅的 paper account

**验证方法**：
在另一台机器上检查：
```bash
cat configs/alpaca_acounts/alpaca_accounts.local.json
```
看 `base_url` 是：
- `https://paper-api.alpaca.markets` → paper account
- `https://api.alpaca.markets` → live account

### 2. 另一台机器的数据请求日期更早
Alpaca 免费版对**最近数据**有限制（15 分钟延迟，或者最近几天）。

如果另一台机器运行时：
- 请求的是更早的历史数据（比如上周、上个月）
- 可能成功

而我们刚才测试：
- `--date 2026-06-27`（今天）
- 需要拉取截至今天的历史 bars
- 触发"最近数据"限制

**验证方法**：
用更早的日期测试：
```bash
python src/alpaca_executor.py \
  --date 2026-06-01 \
  --trigger-mode plan_only \
  --no-submit \
  --feed iex \
  --dynamic-feed iex \
  --output-root artifacts/test_old_date
```

### 3. IEX feed 对 paper account 也有历史数据限制
即使用 IEX feed，Alpaca 免费 paper account 可能：
- 只能访问最近几天的 IEX 数据
- 更早的历史需要付费订阅

### 4. API key 的订阅级别不同
你给的这个 API key:
```
PK2OSKG2BJVN4TG7IA7YAWTKJ6
```
可能是：
- 新注册的免费 paper account
- 没有任何历史数据权限

而另一台机器的 API key 可能：
- 是老账户（grandfathered 权限）
- 或者订阅过数据服务

## 诊断步骤

### Step 1: 检查另一台机器的配置
```bash
# 在另一台机器上运行
cat configs/alpaca_acounts/alpaca_accounts.local.json
# 检查 base_url 和 api_key
```

### Step 2: 尝试更早的日期
```bash
# 在本机尝试
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate

# 测试 1 周前
python src/alpaca_executor.py \
  --date 2026-06-20 \
  --trigger-mode plan_only \
  --no-submit \
  --feed iex \
  --dynamic-feed iex \
  --output-root artifacts/test_old_date
```

### Step 3: 直接测试 Alpaca API
创建最小测试脚本 `test_alpaca_data.py`:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from vendors.alpaca import AlpacaHttpClient, AlpacaCredentials
import json

# 读取配置
with open("configs/alpaca_acounts/alpaca_accounts.local.json") as f:
    config = json.load(f)["ALPACA_US_FULL"]

creds = AlpacaCredentials(
    api_key=config["api_key"],
    api_secret=config["secret_key"],
    base_url="https://paper-api.alpaca.markets",
    data_base_url="https://data.alpaca.markets",
    request_timeout_seconds=60,
    max_retries=3
)

client = AlpacaHttpClient(creds)

print("Testing IEX feed access...")
try:
    bars = client.get_stock_bars(
        symbols=["AAPL"],
        start="2026-06-01",
        end="2026-06-20",
        timeframe="1Day",
        feed="iex",
        limit=100
    )
    print(f"✓ SUCCESS: Got {len(bars)} bars for AAPL")
    for bar in bars[:3]:
        print(f"  {bar}")
except Exception as e:
    print(f"✗ FAILED: {e}")

print("\nTesting account info...")
try:
    account = client.get_account()
    print(f"✓ Account: {account.get('account_number')}")
    print(f"  Status: {account.get('status')}")
    print(f"  Equity: {account.get('equity')}")
except Exception as e:
    print(f"✗ FAILED: {e}")
```

运行:
```bash
python test_alpaca_data.py
```

## 最可能的答案

**另一台机器能跑是因为它用的是 live account**，即使余额很小。Live account 的数据权限远好于 paper account。

你可以：
1. 在另一台机器上确认配置
2. 或者在本机也切换到 live account（小额测试，$100 即可）
3. 或者升级 Alpaca 数据订阅

---

**要我创建 `test_alpaca_data.py` 帮你直接测试 API 可访问性吗？**
