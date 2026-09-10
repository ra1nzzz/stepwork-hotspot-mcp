# stepwork-hotspot-mcp

STEPWORK 的**上游热点发现** MCP Server。独立仓库、独立安装、零运行时依赖（stdlib only）。

> 唯一的例外是热点宝（抖音）：它无公开 API，只能走 CDP 复用用户已登录的
> 浏览器，需要 playwright。故列为**可选依赖** `[browser]`，不装时仅该源报错，
> 其余 11 个免登录源照常可用。

> 它不知道 STEPWORK 存在。STEPWORK 通过既有的 `AddMcpServer` / `CallMcpTool`
> 接它 —— 换掉本服务不需要改 STEPWORK 一行代码。

## 为什么独立成一个仓库

热点源的形态变化比产品快得多（榜单站点改版、反爬升级、第三方镜像挂掉）。
把它塞进主仓会让「源挂了」变成「产品发版」。独立仓库 = 独立发版、独立回滚。

## 装与跑

```bash
pip install -e .                    # 11 个免登录源即可用
pip install -e '.[browser]'         # + 热点宝（抖音），需 CDP
playwright install chromium         # 仅 [browser] 需要
stepwork-hotspot-mcp                # stdio MCP：一行一个 JSON-RPC 2.0，不带 Content-Length
```

## 工具

| 工具 | 入参 | 返回 |
|---|---|---|
| `list_sources` | — | 源的 id / 形态 / 是否需密钥 / **是否需登录** / 备注 |
| `discover_hotspots` | `sources?`, `limit`(1-100, 默认 20), `windowHours`(默认 48), `query?` | `{items[], errors[], count, sources[]}` |
| `list_douhot_boards` | — | 热点宝可抓榜单（需浏览器已登录） |

`items[]` 形状：

```json
{
  "id": "sha256[:16]",
  "source": "github_trending",
  "title": "owner/repo",
  "url": "https://github.com/owner/repo",
  "summary": "…",
  "publishedAt": "2026-09-09T00:00:00+00:00",
  "score": 4624.0,
  "meta": {"language": "Python", "rank": 1}
}
```

**某个源失败不会让整个调用失败**，但一定出现在 `errors[]` 里 —— 静默少给
一半数据比直接报错更难查。

## 数据源：2026-09-10 实测（含失败原因归类）

> 第一版（09-09）曾结论「中文热搜拿不到」。**该结论是错的**：那次用的是
> 第三方镜像当「微博热搜」的代表、没试官方接口、且没跟重定向。**教训：判源
> 死活要先分清「对方不给你」和「你没敲对门」。**

| 源 | 结果 | 失败原因归类 |
|---|---|---|
| **今日头条热榜** `toutiao_hot` | ✅ | **官方**接口，免登录免密钥，50 条中文热点 + HotValue + Label。中文侧最稳 |
| **NewsNow 五榜** `newsnow_{weibo,zhihu,toutiao,baidu,bilibili}` | ✅ | 开源聚合（社区公共实例，可自部署）。微博/知乎/头条/百度/B站一次到手 |
| **抖音热榜** `douyin_hot` | ✅ | 官方 web 接口，免登录免密钥，50 条中文热榜词 + 热度值 |
| **InfoQ** `rss` | ✅ | 公开 RSS；**默认客户端 UA 会被 WAF 回 451**，换浏览器 UA 即 200 |
| **arXiv** `arxiv_latest` | ✅ | 官方 Atom API；必须 https（http 会 301，不跟重定向就是 000） |
| Hugging Face Daily Papers | ✅ | 免密钥；AI 论文，英文 |
| GitHub Trending | ✅ | 免密钥；抓 HTML，结构会变 |
| 少数派 RSS | ✅ | 免密钥；中文，偏效率工具/数码 |
| 今日热榜 `tophub.today` | 🟡 可抓未接 | 服务端渲染（HTML 1.1MB，榜单条目 ~3000 处），**能抓但成本高于 NewsNow**，且覆盖面已被覆盖。它另有付费 API（tophubdata.com） |
| RSSHub 公共实例 | ❌ | **对方反爬**：Cloudflare 403 不限 UA，需自建实例 |
| 36氪 `/feed` | ❌ | **源本身没了**：该端点已改版成 HTML 页，不再是 RSS |
| 微博官方 ajax / 移动版 | ❌ | `Forbidden` / 432：要 cookie + 风控。**改用 NewsNow 的微博榜绕开** |
| **抖音热点宝** `douhot` / `douhot_low_fans` | 🔐 需登录 | **没有公开 API**：页面是微前端 SPA，数据全在登录后带 `a_bogus` 签名的 XHR 里。绕不过，也不该绕（那是绕风控）。走 CDP 复用用户已登录浏览器，见下节 |

**结论（修正后）**：中文热点**拿得到**，而且不止一条 —— 头条（官方）、
抖音（官方）、NewsNow 五榜（微博/知乎/头条/百度/B站）全免密钥。
真正的限制只有两条：微博/百度这类「热搜榜」**没有免登录的官方接口**
（绕法是走 NewsNow 聚合）；RSSHub 这类公共聚合必须自建。

## 热点宝（抖音）接入：走 CDP，不爬接口

热点宝的价值高于抖音热榜：它给的是**创作者视角**的数据（低粉爆款榜、
200+ 垂类榜、热词 60 日趋势、官方活动日历），而不只是「今天大家都在搜什么」。

但它没有公开 API，也不提供个人可申请的开放能力（抖音开放平台要企业资质）。
所以这里**不尝试伪造签名**，而是复用用户**自己已登录**的浏览器：

```bash
# 1) 用调试端口启动浏览器（Edge 示例；Chrome 同理）
msedge.exe --remote-debugging-port=9222

# 2) 在这个浏览器里打开 https://douhot.douyin.com 并完成登录（扫码即可）

# 3) 之后抓取全部由 MCP Server 完成；换端口用 DOUHOT_CDP_ENDPOINT 覆盖
```

边界（写清楚，避免误用）：

- **只读**：只 `goto` + 读响应体/可见文本。不填表、不点按钮、不下载、
  不执行页面脚本，也不读取/导出 cookie。
- `browser.close()` 对 `connect_over_cdp` 只是**断开 CDP 连接**，不会关掉
  用户的浏览器。
- 未登录时**显式报错**（"可能是未登录"），不静默返回空列表 —— 静默空
  结果会被上层当成「今天没热点」。
- 解析先拦 JSON 响应（准），拦不到才退到可见文本（糙），并在
  `meta.parse` 里如实标注走了哪条路（`json` / `text-fallback`）。
- 榜单页面结构与字段会变，故解析只假设「元素里有标题键」，不硬编码选择器。

**配额策略（重要）**：`discover` 按**源**分配额并**交错**输出，不做全局排序。
因为不同源的 score 量纲不可比（抖音千万级 vs GitHub star 千级 vs 无分值），
且「用抓取时刻当时间」的源会永远排在最前 —— 实测头条 50 条吃满 limit=40，
微博/知乎/B 站一条不剩。同理 NewsNow 的每个榜单注册成**独立源**，
否则一个源内部的「更新时间最新榜单」会把配额吃光。

**耗时**：11 个免登录源串行抓取约 20–25s（未并发）；热点宝另计（单次 3–8s，
取决于 SPA 出数据速度）。

## 已知边界

- GitHub Trending 是 HTML 抓取：页面改版就取不到。失败表现为「该源今天没数据」
  （进 `errors[]`），不抛异常。
- HF 论文的时间取 `submittedOnDailyAt`（上今日榜的时刻）而非论文 `publishedAt`
  —— 后者常是几天前，按「最近 48 小时」过滤会把整个源过滤光。
- 缺中文 AI 资讯源（机器之心 RSS 302）。要补的话优先找**有官方 RSS** 的源。
  （InfoQ 曾记 451，实为默认 UA 触发 WAF，换浏览器 UA 后已可用。）
- 热点宝的两个榜**默认不参与** `discover()` 的全源抓取（要连浏览器、要登录，
  不该让「没开浏览器」变成每次调用都报错）；需要时显式传
  `sources: ["douhot", ...]`。上层（STEPWORK）把它做成用户勾选的可选项。

## 测试

```bash
pytest -q      # 18 例，离线（固定件 + 假抓取），不打网络
ruff check . && mypy src tests
```

## License

AGPL-3.0-or-later（与 STEPWORK 一致）。
