# 美股多因子多空策略 Phase7K 技术报告

基于 Alpaca 日线、SEC XBRL 财务数据与 locked-lot 持仓管理的市场中性回测框架

量化策略研究笔记

2026 年 6 月 8 日

## 摘要

本文系统整理一套面向美股普通股横截面交易的多因子 alpha、动态股票池、市场中性组合优化与 locked-lot 持仓管理框架。策略核心并非单纯选择高分股票，而是将“选股信号”和“持仓可交易性”拆解为两个同等重要的层次：第一层从价格、风险和财务质量中构造行业内可比的横截面分数；第二层在多空两侧分别建仓，并通过锁仓下界、单名权重上限、beta 中性、行业暴露惩罚和换手预算约束生成可执行目标仓位。

当前生产版本使用 Alpaca 日线作为价格与成交额来源，使用 SEC companyfacts / submissions 作为财务数据来源，股票池由固定候选表、资产可交易状态、普通股过滤、价格门槛、历史 bar 数和滞后流动性共同确定。回测采用 open-to-open 交易口径，并显式计入执行成本、SEC fee 与 TAF。最新主回测区间为 2017-01-03 至 2026-05-19，主资金层级初始权益 10,000 美元，期末归一化净值 6.80，年化收益 22.74%，最大回撤 -26.53%，零利率夏普 1.36；50,000 美元层级年化收益 23.92%，最大回撤 -23.98%，零利率夏普 1.44。回测对比与多资金层级结果列入附录。

关键词：美股多空；多因子选股；市场中性；动态股票池；SEC XBRL；beta 中性；换手约束；locked-lot；无未来函数

## 1 研究目标与策略结构

本项目的核心目标是构建一套可从研究回测平滑迁移到实盘执行的美股多空组合系统。与只输出股票排名的纯策略不同，本系统将每日交易决策拆成四个模块：

1. 交易哪些标的：动态股票池过滤，确保候选标的为可交易、非 ETF/ADR/特殊证券且具有足够历史数据和流动性；
2. 如何评分：在行业内标准化五个 alpha 因子，合成为单日横截面分数；
3. 如何成组合：用线性规划同时决定多头与空头权重，约束 beta、行业暴露、单名集中度和换手；
4. 如何延续持仓：使用 locked-lot ledger 将仓位拆分为由不同因子支持的批次，并给不同因子设置最短持有期。

策略族可记为 Phase7K：

- Pool：动态股票池构造层；
- Alpha：五因子横截面评分层；
- Optimizer：多空组合优化层；
- Lot：locked-lot 持仓管理层；
- Executor：将目标权重转换为 broker order plan 的执行层。

## 2 数据源与样本构造

### 2.1 原始数据链路

本文使用的生产数据链路为：

```text
Alpaca assets + Alpaca daily bars + SEC companyfacts/submissions -> alpha panel -> decision targets -> execution plan.
```

其中：

- Alpaca assets：标的资产状态、是否可交易、是否可做空等交易属性；
- Alpaca daily bars：日线 open/high/low/close/volume，用于收益、动量、beta 和流动性估计；
- SEC companyfacts：资产、负债、现金、收入、利润、经营现金流、股份数等 XBRL 财务字段；
- SEC submissions：补充财报 filing date、period end 与表单类型；
- 静态或动态行业映射：当前使用 SIC 二位行业作为行业中性化分组。

### 2.2 动态股票池

令固定候选列表为 \(C_0\)，Alpaca 在日期 \(t\) 可见的资产集合为 \(A_t\)。经过证券类型过滤后的普通股核心集合为

\[
C_t^{core}=\{i\in C_0\cap A_t: i\notin ETF,\ i\notin ADR,\ i\notin Warrant/Unit/Preferred,\ i\text{ tradable}\}. \tag{1}
\]

对每个标的只使用 \(t\) 日之前的日线数据。记 \(B_{i,t}^{-}\) 为 \(i\) 在 \(t\) 前全部可用 bar 数，\(P_{i,t-1}\) 为最近一个前日收盘价，\(DV_{i,k}=P_{i,k}V_{i,k}\) 为成交额。近 20 个交易日滞后中位成交额为

\[
\widetilde{DV}_{i,t}^{20}
= \operatorname{Median}\{DV_{i,k}: k<t,\ k\in \text{last 20 sessions of }i\}. \tag{2}
\]

当前可进入股票池的条件为

\[
\Omega_t=\{i\in C_t^{core}: B_{i,t}^{-}\ge 252,\ \#\text{obs}_{20}(i,t)\ge 15,\ P_{i,t-1}\ge 10,\ \widetilde{DV}_{i,t}^{20}>0\}. \tag{3}
\]

最终动态股票池取滞后流动性最高的前 1000 只：

\[
U_t=\operatorname{TopK}_{i\in\Omega_t}\left(\widetilde{DV}_{i,t}^{20};K=1000\right). \tag{4}
\]

上述定义保证股票池在日期 \(t\) 的构造只依赖 \(t\) 之前可观察信息。

## 3 Alpha 因子定义

### 3.1 价格与风险因子

令 \(C_{i,t}\) 为收盘价，\(r_{i,t}=C_{i,t}/C_{i,t-1}-1\)。短期反转因子使用 5 日收益的相反数：

\[
f^{rev}_{i,t}=-\left(\frac{C_{i,t}}{C_{i,t-5}}-1\right). \tag{5}
\]

中期动量因子跳过最近 20 个交易日，使用 120 日至 20 日区间收益：

\[
f^{mom}_{i,t}=\frac{C_{i,t-20}}{C_{i,t-140}}-1. \tag{6}
\]

令 \(r^m_t\) 为基准指数收益，当前基准为 SPY。使用 252 日滚动窗口、至少 126 个观测，并滞后一日估计 beta：

\[
\beta^{raw}_{i,t}
=\frac{\operatorname{Cov}_{k\in[t-252,t-1]}(r_{i,k},r^m_k)}
{\operatorname{Var}_{k\in[t-252,t-1]}(r^m_k)}. \tag{7}
\]

对 beta 做向 1.0 的收缩，收缩强度 \(\eta=0.10\)，并裁剪至 \([0,3]\)：

\[
\beta_{i,t}=\operatorname{clip}\left((1-\eta)\beta^{raw}_{i,t}+\eta,\ 0,\ 3\right). \tag{8}
\]

低 beta 因子定义为

\[
f^{low\_beta}_{i,t}=-\beta_{i,t}. \tag{9}
\]

### 3.2 规模与财务质量因子

令 \(SO_{i,t}\) 为 SEC 可得的股份数，\(P_{i,t-1}\) 为滞后收盘价。市值与小规模因子为

\[
MC_{i,t}=SO_{i,t}P_{i,t-1},\qquad
f^{size}_{i,t}=-\log(MC_{i,t}). \tag{10}
\]

令 \(Cash_{i,t}\) 为现金及短期投资，\(Assets_{i,t}\) 为总资产，现金质量因子为

\[
f^{cash}_{i,t}=\frac{Cash_{i,t}}{Assets_{i,t}}. \tag{11}
\]

### 3.3 行业内标准化与综合分数

设五个原始因子集合为

\[
\mathcal{F}=\{rev,mom,size,low\_beta,cash\}. \tag{12}
\]

对每个交易日 \(t\)、行业 \(g\)、因子 \(a\)，进行行业内 z-score：

\[
z^a_{i,t}=
\frac{f^a_{i,t}-\mu^a_{g(i),t}}
{\sigma^a_{g(i),t}},\qquad i\in U_t. \tag{13}
\]

若行业内标准差不可用，则回退到全市场同日 z-score；所有 z-score 裁剪至 \([-3,3]\)。当前因子权重为

\[
w_{rev}=0.25,\quad w_{mom}=0.10,\quad w_{size}=0.30,\quad
w_{low\_beta}=0.20,\quad w_{cash}=0.15. \tag{14}
\]

原始综合分数为

\[
s^{raw}_{i,t}=
\frac{\sum_{a\in\mathcal{F}}w_a z^a_{i,t}}
{\sum_{a\in\mathcal{F}}|w_a|}. \tag{15}
\]

再对当日全市场横截面做一次 z-score，得到最终 alpha 分数：

\[
s_{i,t}=Z_t(s^{raw}_{i,t}). \tag{16}
\]

## 4 多空候选集合

在每个交易日 \(t\)，多头候选优先选择综合分数高的股票，空头候选优先选择综合分数低的股票。令上一日权重集合为 \(P^L_{t-1},P^S_{t-1}\)，锁定仓位集合为 \(K^L_t,K^S_t\)。为避免因优化器丢失仍处于锁定期的仓位，候选集合定义为“高分/低分候选 + 既有持仓 + 锁定持仓”的并集：

\[
\mathcal{L}_t =
\operatorname{TopK}_{i\in U_t\setminus K^S_t}(s_{i,t};K=120)
\cup P^L_{t-1}\cup K^L_t, \tag{17}
\]

\[
\mathcal{S}_t =
\operatorname{BottomK}_{i\in U_t\setminus K^L_t}(s_{i,t};K=120)
\cup P^S_{t-1}\cup K^S_t. \tag{18}
\]

若多头或空头候选少于 20 个，则系统优先尝试 carry previous；若没有可 carry 的完整组合，则跳过当日决策。

## 5 组合优化模型

### 5.1 权重变量

令多头候选数为 \(n_L\)，空头候选数为 \(n_S\)，总数 \(n=n_L+n_S\)。优化变量为

\[
x_t=(x^L_{1,t},\ldots,x^L_{n_L,t},x^S_{1,t},\ldots,x^S_{n_S,t})^\top. \tag{19}
\]

这里 \(x^L\) 与 \(x^S\) 均为正的 side weight，实际组合 signed weight 为多头 \(+x^L\)、空头 \(-x^S\)。当前约束每侧满仓：

\[
\sum_{i\in\mathcal{L}_t}x^L_{i,t}=1,\qquad
\sum_{j\in\mathcal{S}_t}x^S_{j,t}=1. \tag{20}
\]

因此组合 gross exposure 为 2，净名义敞口为 0。

### 5.2 锁仓下界与单名上限

令 ledger 中仍处于锁定期的权重为 \(\ell_{i,t}^{side}\)。优化必须继承这些锁定仓位：

\[
x^{side}_{i,t}\ge \ell^{side}_{i,t}. \tag{21}
\]

单名 side weight 上限为

\[
0\le x^{side}_{i,t}\le \bar{x},\qquad \bar{x}=1/30. \tag{22}
\]

该约束使单只股票在某一侧的目标权重不超过 3.33%，防止单名贡献过度集中。

### 5.3 Beta 中性约束

严格 beta 中性版本为

\[
\sum_{i\in\mathcal{L}_t}x^L_{i,t}\beta_{i,t}
-\sum_{j\in\mathcal{S}_t}x^S_{j,t}\beta_{j,t}=0. \tag{23}
\]

若严格约束不可行，则按网格 \(\delta_\beta\in\{0.05,0.10,0.15,0.20\}\) 逐步放松：

\[
\left|
\sum_{i\in\mathcal{L}_t}x^L_{i,t}\beta_{i,t}
-\sum_{j\in\mathcal{S}_t}x^S_{j,t}\beta_{j,t}
\right|\le \delta_\beta. \tag{24}
\]

### 5.4 换手预算

令上一期同一候选向量上的权重为 \(p_t\)，新组合的 raw turnover 为

\[
\tau^{raw}_t=\sum_{k=1}^{n}|x_{k,t}-p_{k,t}|. \tag{25}
\]

若上一期组合未充分部署，部署缺口为

\[
d_t=\max\left(0,2-\sum_i p^L_{i,t}-\sum_j p^S_{j,t}\right). \tag{26}
\]

当日换手约束为

\[
\tau^{raw}_t\le B+d_t,\qquad B=0.15. \tag{27}
\]

报告口径下的 rebalancing turnover 为

\[
\tau_t=\max(0,\tau^{raw}_t-d_t). \tag{28}
\]

回测执行成本中使用成交名义的一半折算组合换手：

\[
\tau^{bt}_t=\frac{1}{2}\frac{\text{trade notional}_t}{E_t}. \tag{29}
\]

### 5.5 行业暴露惩罚

设 \(G\) 为行业暴露矩阵，行业 \(q\) 的净 side exposure 为

\[
e_{q,t}=
\sum_{i\in\mathcal{L}_t:g(i)=q}x^L_{i,t}
-\sum_{j\in\mathcal{S}_t:g(j)=q}x^S_{j,t}. \tag{30}
\]

优化器引入松弛变量 \(v_{q,t}\ge |e_{q,t}|\)，并在目标函数中惩罚行业净暴露。

### 5.6 线性规划目标函数

令多头得分为 \(a^L_{i,t}=Z_t(s_{i,t})\)，空头得分为 \(a^S_{j,t}=Z_t(-s_{j,t})\)，合并为 \(a_t\)。引入换手绝对值变量 \(u_t\) 与行业暴露变量 \(v_t\)，当前线性规划为

\[
\min_{x,u,v}
-\lambda_s a_t^\top x
+\lambda_\tau \mathbf{1}^\top u
+\lambda_g \mathbf{1}^\top v, \tag{31}
\]

其中

\[
\lambda_s=0.01,\qquad \lambda_\tau=0.005,\qquad \lambda_g=25.0. \tag{32}
\]

约束集合为

\[
\begin{aligned}
&\mathbf{1}^\top x^L=1,\quad \mathbf{1}^\top x^S=1,\\
&\beta_L^\top x^L-\beta_S^\top x^S=0 \quad \text{或满足式 }(24),\\
&\ell_t\le x_t\le \bar{x},\\
&-u_t\le x_t-p_t\le u_t,\quad \mathbf{1}^\top u_t\le B+d_t,\\
&-v_t\le Gx_t\le v_t,\quad u_t\ge0,\quad v_t\ge0.
\end{aligned} \tag{33}
\]

该模型说明：策略并不是简单买 top、卖 bottom，而是在交易成本、持仓延续和风险中性条件下寻找“可成交的 alpha 最大化”组合。

## 6 Locked-lot 持仓管理

### 6.1 Lot 状态变量

ledger 将每个 side 的仓位拆成多个 lot。一个 lot 定义为

\[
q=(i,a,w,b,h,side), \tag{34}
\]

其中 \(i\) 为股票，\(a\) 为支持该仓位的因子，\(w\) 为 side weight，\(b\) 为建仓 session index，\(h\) 为最短持有期。若

\[
t-b<h, \tag{35}
\]

则该 lot 在日期 \(t\) 处于锁定状态。当前各因子最短持有期为

\[
h_{rev}=5,\quad h_{mom}=10,\quad h_{size}=20,\quad h_{low\_beta}=20,\quad h_{cash}=20. \tag{36}
\]

### 6.2 因子支持权重拆分

当目标权重中出现新增残差 \(\Delta w_{i,t}^{side}>0\) 时，需要判断该新增仓位由哪些因子支持。对多头，方向得分为 \(z^a_{i,t}\)；对空头，方向得分为 \(-z^a_{i,t}\)。因子支持强度为

\[
\phi^{a,side}_{i,t}=
\max\left(0,w_a d^{side}z^a_{i,t}\right),\qquad
d^L=1,\ d^S=-1. \tag{37}
\]

若 \(\sum_a\phi^{a,side}_{i,t}>0\)，则新增 lot 的因子份额为

\[
\rho^{a,side}_{i,t}=
\frac{\phi^{a,side}_{i,t}}
{\sum_{b\in\mathcal{F}}\phi^{b,side}_{i,t}}. \tag{38}
\]

若所有方向支持强度均为 0，则回退为按正因子权重分配：

\[
\rho^{a,side}_{i,t}=\frac{w_a}{\sum_{b\in\mathcal{F}}w_b}. \tag{39}
\]

新增 lot 权重为

\[
w^{new,a,side}_{i,t}=\Delta w_{i,t}^{side}\rho^{a,side}_{i,t}. \tag{40}
\]

### 6.3 Ledger 更新规则

令目标 side weight 为 \(x_{i,t}^{side}\)，同一股票上一期 lot 集合为 \(Q_{i,t-1}^{side}\)。更新规则分三步：

1. 先保留全部锁定 lot，其权重形成优化下界 \(\ell^{side}_{i,t}\)；
2. 对已过锁定期的 lot，按原有 lot 顺序最多保留到目标权重；
3. 若仍有剩余目标权重，则按式 (37)--(40) 创建新 lot。

用数学形式表示，保留锁定权重为

\[
L^{side}_{i,t}=\sum_{q\in Q_{i,t-1}^{side}}w_q\mathbf{1}(t-b_q<h_q). \tag{41}
\]

可自由保留的过期 lot 权重上限为

\[
E^{keep,side}_{i,t}=
\min\left(
\sum_{q\in Q_{i,t-1}^{side}}w_q\mathbf{1}(t-b_q\ge h_q),
\max(0,x^{side}_{i,t}-L^{side}_{i,t})
\right). \tag{42}
\]

新增权重为

\[
\Delta w^{side}_{i,t}=
\max\left(0,x^{side}_{i,t}-L^{side}_{i,t}-E^{keep,side}_{i,t}\right). \tag{43}
\]

因此 locked-lot 机制把“信号希望立即换仓”和“持仓应该有最短验证期”统一到一个可计算的约束系统中。它是本项目区别于纯 top/bottom 策略的关键部分。

## 7 交易执行与成本模型

目标权重会转化为 signed notional，再根据参考价格、账户权益和 broker 约束生成订单。回测成本模型包括：

\[
\text{execution cost}_t = 8\text{ bps}\times \text{trade notional}_t, \tag{44}
\]

\[
\text{SEC fee}_t = 2.78\times10^{-5}\times \text{sell notional}_t, \tag{45}
\]

\[
\text{TAF}_t = \min(0.000195\times \text{sell shares}_t,\ 9.79\text{ per trade}). \tag{46}
\]

日收益按交易后组合在下一开盘价重估，成本直接从权益中扣除：

\[
R^{net}_{t+1}=\frac{E_{t+1}}{E_t}-1,\qquad
R^{gross}_{t+1}=\frac{E_{t+1}+\text{cost}_t}{E_t}-1. \tag{47}
\]

## 8 回测设置与当前结果

当前主回测使用 open-to-open 口径，报告期为 2017-01-03 至 2026-05-19，共 2357 个交易区间。暖身期为 252 个 session。主结果使用最新目录：

```text
artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun
```

主要结果如下：

| 指标 | 策略 10,000 | SPY | QQQ |
|---|---:|---:|---:|
| 期末净值 | 6.80 | 3.76 | 6.29 |
| 总收益 | 579.63% | 276.01% | 529.50% |
| 年化收益 | 22.74% | 15.21% | 21.74% |
| 年化波动 | 15.97% | 17.56% | 22.23% |
| 最大回撤 | -26.53% | -32.05% | -36.58% |
| 零利率夏普 | 1.36 | 0.90 | 1.00 |

组合运行统计显示，平均动态股票池约 815 只，平均回测换手约 6.03%，平均 gross exposure 约 2.00，net exposure 数值上接近 0。换手最高值主要出现在初始建仓日，随后由 locked-lot 与 turnover budget 抑制。

## 9 超参数设定与选定依据

| 参数 | 当前取值 | 选定依据与作用 |
|---|---:|---|
| 动态池规模 | 1000 | 覆盖足够横截面，同时保留流动性筛选 |
| 流动性窗口 | 20 日 | 用滞后中位成交额降低单日异常影响 |
| 最少近端观测 | 15 | 保证 20 日流动性估计可用 |
| 最少历史 bar | 252 | 兼顾 beta 估计和样本成熟度 |
| 价格门槛 | 10 美元 | 避免低价股交易摩擦和做空约束失真 |
| beta 窗口 | 252 日 | 约一年交易日，用于市场风险估计 |
| beta 最少观测 | 126 | 低于半年观测不参与 beta 估计 |
| beta 收缩强度 | 0.10 | 缓和样本 beta 噪声 |
| beta 裁剪 | [0, 3] | 限制异常 beta 对优化器的支配 |
| 候选池每侧 | 120 | 给优化器足够选择空间 |
| 每侧最少非零名数 | 20 | 防止组合过度集中 |
| 单名 side weight 上限 | 1/30 | 单名最多约 3.33% |
| 换手预算 | 0.15 | 限制每日再平衡幅度 |
| alpha 得分权重 | 0.01 | alpha 与风险/换手惩罚同处一个 LP 目标 |
| 换手惩罚 | 0.005 | 在预算内仍偏好更少交易 |
| 行业惩罚 | 25.0 | 强化行业中性，降低行业方向暴露 |
| beta 放松网格 | 0.05/0.10/0.15/0.20 | 严格中性不可行时逐步放松 |
| 因子最短持有 | 5/10/20/20/20 | 反转更快，规模、beta、财务质量更慢 |

## 10 风险与后续改进

当前版本仍有以下限制：

1. SEC 财务字段存在披露延迟和 tag 选择问题，后续应更严格建模 filing lag；
2. 回测成本包含固定 bps、SEC fee 和 TAF，但仍未完全刻画冲击成本、借券费和 hard-to-borrow 约束；
3. 空头执行在小资金层级会受到整股和最小交易金额影响，多层级结果已部分反映此问题；
4. 行业使用 SIC 二位码，后续可比较 GICS/NAICS 或 broker 行业分类；
5. 当前因子权重为研究先验，尚未进行 walk-forward 参数稳定性检验；
6. locked-lot 的最短持有期来自交易逻辑设定，后续应检验其对换手、回撤和 alpha 衰减的边际贡献。

后续正式参数研究建议使用 walk-forward 框架，例如训练 3--5 年、测试 1 年，滚动评估因子权重、候选池大小、换手预算、单名权重上限、beta band 与 min-hold。评价指标不应只看收益，还应同时约束最大回撤、换手、成本后收益、借券可得性和分层资金容量。

## 附录 A 回测对比数据

### A.1 多资金层级结果

| 初始权益 | 期末归一化净值 | 总收益 | 年化收益 | 最大回撤 | 年化波动 | 零利率夏普 |
|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 6.80 | 579.63% | 22.74% | -26.53% | 15.97% | 1.36 |
| 50,000 | 7.43 | 643.26% | 23.92% | -23.98% | 15.71% | 1.44 |
| 100,000 | 7.34 | 633.82% | 23.75% | -24.15% | 15.71% | 1.43 |
| 300,000 | 7.22 | 621.56% | 23.53% | -24.20% | 15.72% | 1.42 |

### A.2 成本模型参数

| 成本项 | 当前取值 |
|---|---:|
| 执行成本 | 8 bps |
| 卖出名义比例 | 0.5 |
| SEC fee rate | 0.0000278 |
| TAF 每股 | 0.000195 |
| TAF 单笔上限 | 9.79 |

### A.3 组合运行统计

| 指标 | 均值 | 中位数 | 25% 分位 | 75% 分位 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 回测换手 | 6.03% | 6.21% | 4.99% | 7.17% | 94.15% |
| 动态股票池数量 | 814.62 | 824 | 751 | 875 | 934 |
| 多头持仓名数 | 65.18 | 65 | 60 | 70 | 86 |
| 空头持仓名数 | 71.70 | 72 | 66 | 77 | 93 |
| Gross exposure | 2.00 | 2.00 | 2.00 | 2.00 | 2.00 |
| Net exposure | 约 0 | 0 | 约 0 | 约 0 | 约 0 |

### A.4 主要产物

| 文件 | 内容 |
|---|---|
| `artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/backtest_summary.json` | 主回测摘要 |
| `artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/daily_backtest_results.csv` | 日度收益、换手、成本、持仓统计 |
| `artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/equity_curve_compare.png` | 策略与 SPY/QQQ 净值曲线 |
| `artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/drawdown_curve_compare.png` | 策略与 SPY/QQQ 回撤曲线 |
| `artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/final_lot_ledger.json` | 回测结束时 locked-lot ledger |

