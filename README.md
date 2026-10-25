# 价格轨迹

每天自动抓一次商品价格，存进这个仓库，用网页看曲线。全部跑在 GitHub 免费额度内。

```
products.json                      你要追踪的商品清单（你改这个）
data/prices.json                   抓到的价格历史（脚本自动写，别手改）
scripts/fetch_prices.py            抓价脚本
.github/workflows/track-prices.yml 定时任务
index.html                         看板页面
```

---

## 一次性安装（约 10 分钟）

### 1. 建仓库

GitHub 右上角 **+** → **New repository**。

- 名字填 `price-tracker`
- 选 **Private**（之后也能改公开）
- **不要**勾选任何「Add a README / .gitignore / license」
- 点 **Create repository**

### 2. 传文件

在空仓库页面点 **uploading an existing file**，把这几个文件拖进去：

- `products.json`
- `index.html`
- `README.md`

然后点 **Commit changes**。

剩下三个在子目录里的文件，拖拽有时会丢掉目录结构，用「新建文件」更稳妥。**重复下面三次**：

点 **Add file** → **Create new file**，在文件名框里粘贴完整路径（打斜杠会自动建目录），再把内容粘进去，提交。

| 文件名框里填 | 内容来自 |
|---|---|
| `scripts/fetch_prices.py` | 同名文件 |
| `.github/workflows/track-prices.yml` | 同名文件 |
| `data/prices.json` | 同名文件 |

### 3. 允许 Actions 写仓库

**Settings** → 左栏 **Actions** → **General** → 翻到最下面 **Workflow permissions**
→ 选 **Read and write permissions** → **Save**。

没这一步，脚本抓到价格也提交不回来。

### 4. 开 GitHub Pages

**Settings** → 左栏 **Pages** → Source 选 **Deploy from a branch**
→ 分支选 `main`、目录选 `/ (root)` → **Save**。

等一两分钟，页面地址是：

```
https://<你的用户名>.github.io/price-tracker/
```

### 5. 手动跑一次，验证能用

**Actions** → 左栏点 **抓价** → 右边 **Run workflow** → 绿色按钮。

跑完点进去看日志：

- `✓ 商品id   22.69  (json-ld)` — 成功
- `✗ 商品id   HTTP 403` — 被对方反爬拦了，见下面「抓不到价格怎么办」

---

## 日常使用

### 加 / 删商品

编辑 `products.json`。在 GitHub 网页上点开文件 → 右上角铅笔图标 → 改 → Commit。

一个商品长这样：

```json
{
  "id": "sony-xm5-jbhifi",
  "name": "Sony WH-1000XM5 降噪耳机",
  "retailer": "JB Hi-Fi",
  "url": "https://www.jbhifi.com.au/products/....",
  "currency": "AUD",
  "target": 399.00,
  "selector": null
}
```

- `id` 只能用字母数字和连字符，**定了就别改**（改了等于换了个新商品，历史价格会对不上）
- `target` 不想设就写 `null`
- `selector` 先一律填 `null`，抓不到时再用，见下

### 改抓取频率

编辑 `.github/workflows/track-prices.yml` 里的 cron。时间是 **UTC**，悉尼夏令时要减 11 小时。

| 想要的效果 | 写法 |
|---|---|
| 每天一次（默认，悉尼 08:42） | `- cron: "42 21 * * *"` |
| 每 4 小时一次（**黑五建议**） | `- cron: "42 */4 * * *"` |
| 每小时一次 | `- cron: "42 * * * *"` |

---

## 抓不到价格怎么办

日志里出现 `✗` 时，按原因对号入座：

### `HTTP 403` / `HTTP 429`

对方把 GitHub 的服务器 IP 当机器人拦了。Amazon、Chewy 这类大站很常见。办法：

1. **换个卖同款的小站追踪** — 最省事，而且中小零售商的价格波动本来就更大、更值得追
2. **用抓取代理** — ScraperAPI、ScrapingBee 之类有免费额度（每月约 1000 次，够每天抓 30 个商品）。需要改脚本，告诉我我来改
3. **走官方 API** — Amazon 有 Product Advertising API，但要求你的 Associates 账号先出单才开通

### `页面里找不到结构化价格`

页面能打开，但价格不在标准位置。给这个商品加 `selector`：

1. 浏览器打开商品页，右键价格数字 → **检查**
2. 在高亮的那行 HTML 上右键 → **Copy** → **Copy selector**
3. 粘到该商品的 `selector` 字段里，比如 `"selector": "span.price-value"`

### `ProxyError` / `Timeout`

网络抖动，下次自动重试即可。连续几天都这样再处理。

---

## 两个容易踩的坑

**仓库 60 天没有任何提交，GitHub 会自动停掉定时任务。** 抓价脚本每天都在提交，正常情况不会触发。但如果连续很多天所有商品都抓失败（没有新数据 = 没有提交），就可能被停。到时候去 Actions 页面点一下 **Enable workflow** 恢复。

**定时任务不保证准点。** GitHub 在整点前后最忙，可能延迟几分钟到半小时。所以 cron 特意设在 `:42` 而不是 `:00`。追价格不需要精确到分钟，不用管它。
