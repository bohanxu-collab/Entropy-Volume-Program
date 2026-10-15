# Entropy Volume Program

**[中文](#zh) · [English](#en)**

<a id="zh"></a>

## 中文

在 Entropy（Hyperliquid HIP-3）的 **`io:SNDK`** 市场上自动刷交易量：用小仓位开仓，然后马上平仓，一直循环。

### 为什么写这个程序，为什么开源

- **省时省力**：不用自己一笔一笔手点，挂在服务器上自己跑。
- **比手刷风险低**：手刷容易点错方向、下错数量、忘记平仓。程序每笔开仓后马上平仓，还有每日亏损上限，到了就自动停。
- **为什么选 SNDK**：它是目前 Entropy io 上交易量最大的市场。

### 先说清楚：这不是赚钱的程序

它只是用来刷量的。每个来回都要付手续费，还要付买卖价差，刷得越多，花得越多。

**开跑之前**：我自己实测刷过 100 万交易量。要付的成本有两块：

- **手续费**：吃单费 0.9 bp，每 100 万交易量约 **90 刀**（我实测是 86 刀）。用下面的邀请链接注册，手续费可以**全部返还**（Entropy 返 50%，邀请人再把他那 50% 返给你），返佣每两周发一次，所以手续费要先垫着。
- **价差**：每笔成交要付一半的买卖价差，这部分不返。SNDK 价差最窄（1 跳）时，每 100 万约 **37 刀**，再加一点开平仓之间的价格波动，合计大约 **40~55 刀**。

算清楚能接受多少成本，再决定刷多少。建议先用小金额跑一天，看日志里实际花了多少钱。

### 邀请链接

还没注册 Entropy 的话，可以用这个邀请链接：**https://entropy.io/?r=yourquantguy**

- 这是 T4 等级的邀请链接。按 Entropy 官方文档，用 T4 链接注册，你付的手续费会返 **50%**，每两周在个人页面领取（满 5 刀才能领）。
- 剩下的 50% 是邀请人的奖励，他会**全部返给你**，注册后找他要就行。
- 为什么用他的链接：我自己没有 T4 的邀请链接；而且我是看了他的一些推文，才对写量化（Quant）脚本感兴趣的。

### 安装

需要 Python 3.9 或以上版本。建议放在一台一直开着的服务器上。Hyperliquid 的服务器在东京，所以东京的云服务器延迟最低。

```bash
git clone https://github.com/bohanxu-collab/Entropy-Volume-Program.git
cd Entropy-Volume-Program
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

#### 1. 建一个 API 钱包（必须做）

打开 app.hyperliquid.xyz，进入 More → API，生成一个 API 钱包，并授权给你的主账户。

- API 钱包**只能下单，不能提币**。就算它的私钥泄露了，别人也拿不走你的钱。
- **绝对不要**把主钱包的私钥放进这个程序。

#### 2. 填写 `.env`

```
HL_API_PRIVATE_KEY=<API 钱包的私钥>
HL_ACCOUNT_ADDRESS=<你的主账户地址，就是放 USDC 的那个，不是 API 钱包的地址>
```

#### 3. 修改 `config.yaml`

最重要的是这三个参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `size_usd` | 60 | 每笔下单的金额 |
| `interval_sec` | 3 | 每两个来回之间等多少秒。每个来回下单本身还要大约 1.5 秒，所以按 3 秒、60 刀算，一天最多大约 230 万交易量 |
| `daily_loss_limit_usd` | 5 | 当天花掉的成本到了这个数就停，UTC 0 点自动恢复 |

其他参数的说明见 `config.example.yaml` 里的注释。

#### 4. 先空跑，再实盘

```bash
python3 churn.py --dry-run     # 只读，不下单，不需要私钥
python3 churn.py               # 实盘
```

#### 停止

- 按 `Ctrl-C`：程序会先平掉仓位，再退出。
- 在程序目录下建一个名叫 `STOP` 的文件：程序会先平仓，再退出（适合在服务器上用）。

程序每 60 秒会打印一行从交易所拉来的真实数字：交易量、手续费、价格盈亏，以及每 100 万的实际成本。

### 安全

- **只用 API 钱包的私钥，永远不要用主钱包的私钥。**
- **`.env` 永远不要上传，也不要发给任何人。** `.gitignore` 已经把它排除了，但你自己 fork 或改代码的时候，注意别手动把它加进去。
- 任何人（包括帮你安装的人）**都不需要你的私钥**。私钥由你自己粘贴进 `.env`。谁找你要私钥，谁就是骗子。
- 如果启动时 SNDK 已经有仓位，程序会拒绝运行，以免误平你手动开的仓。
- 先用小金额跑一天，看看日志里的实际成本和预估是否一致。

### 已知限制

- 平仓用的是 IOC 市价单，行情剧烈波动时可能平不完。程序会重试 3 次；如果还平不掉，就不再开新仓，**需要你手动处理**。
- Entropy 的市场都是逐仓，杠杆只在启动时设置一次。
- 平台规则可能不认这类交易量。积分或奖励以平台规则为准，风险自负。

### 安装服务（给不懂技术的朋友）

如果你完全不会装，我可以远程帮你装好，**50 USD / 人**，装好之后由你自己运行。

联系方式：微信 18696887268，邮箱 xurichard05@gmail.com

安装过程中**我不会、也不需要拿你的私钥**：所有密钥都由你自己在你的电脑或服务器上输入。

MIT 许可证。不提供任何担保，使用风险自负。

---

<a id="en"></a>

## English

**[← 中文](#zh)**

Entropy Volume Program generates trading volume on Entropy's **`io:SNDK`** market (Hyperliquid HIP-3): it opens a small position, closes it immediately, and repeats.

**Why:** it saves you the time of clicking every trade by hand, and it is less risky than manual trading (no wrong side, wrong size or forgotten position; every open is closed right away, and a daily loss limit stops it). SNDK is used because it is currently the highest-volume market on Entropy io.

**This is not a profitable program.** It is only for volume. Every round trip pays fees plus the bid/ask spread. I tested $1M of volume myself. Fees: the 0.9 bp taker fee is about **$90 per $1M** ($86 measured). With the referral link below, fees are **fully refunded** (Entropy pays back 50%, and the referrer hands back his 50%), paid bi-weekly, so you front them first. The spread is not refunded: at SNDK's one-tick spread it is about **$37 per $1M**, about **$40–55** with price drift. Work out what you can accept before you decide how much to run. Start small for a day and check the real cost in the log.

**Referral link:** https://entropy.io/?r=yourquantguy. It is a T4 link: per Entropy's docs you get back **50% of the fees you pay**, claimable bi-weekly from your profile ($5 minimum). The referrer hands back his 50% referral reward too; just ask him after you sign up. I use his link because I do not have a T4 link myself, and his tweets are what got me into writing quant scripts.

### Quick start

```bash
git clone https://github.com/bohanxu-collab/Entropy-Volume-Program.git
cd Entropy-Volume-Program
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml && cp .env.example .env
```

1. On app.hyperliquid.xyz → More → API, create an **API wallet** (it can trade but cannot withdraw).
2. Put its private key and your **main** account address in `.env`.
3. Edit `config.yaml` (`size_usd`, `interval_sec`, `daily_loss_limit_usd`).
4. `python3 churn.py --dry-run`, then `python3 churn.py`. Stop with Ctrl-C or by creating a file named `STOP`.

### Security

- Never use your main wallet's private key. Never share or commit `.env`.
- Nobody, including anyone helping you install, needs your private key. Anyone who asks for it is a scammer.

### Install service

If you cannot install it yourself, I can do it remotely for **50 USD per person**; you run it yourself afterwards. I never take your private key. Contact: WeChat 18696887268, email xurichard05@gmail.com

MIT License. No warranty; use at your own risk.
