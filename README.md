# stepwork-hotspot-mcp

STEPWORK 的**上游热点发现** MCP Server。独立仓库、独立安装、零运行时依赖（stdlib only）。

> 它不知道 STEPWORK 存在。STEPWORK 通过既有的 `AddMcpServer` / `CallMcpTool`
> 接它 —— 换掉本服务不需要改 STEPWORK 一行代码。

## 为什么独立成一个仓库

热点源的形态变化比产品快得多（榜单站点改版、反爬升级、第三方镜像挂掉）。
把它塞进主仓会让「源挂了」变成「产品发版」。独立仓库 = 独立发版、独立回滚。

## 装与跑

```bash
pip install -e .
stepwork-hotspot-mcp          # stdio MCP：一行一个 JSON-RPC 2.0，不带 Content-Length
```

## 工具

| 工具 | 入参 | 返回 |
|---|---|---|
| `list_sources` | — | 源的 id / 形态 / 是否需密钥 / 备注 |
| `discover_hotspots` | `sources?`, `limit`(1-100, 默认 20), `windowHours`(默认 48), `query?` | `{items[], errors[], count, sources[]}` |

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

**结论（修正后）**：中文热点**拿得到**，而且不止一条 —— 头条（官方）、
抖音（官方）、NewsNow 五榜（微博/知乎/头条/百度/B站）全免密钥。
真正的限制只有两条：微博/百度这类「热搜榜」**没有免登录的官方接口**
（绕法是走 NewsNow 聚合）；RSSHub 这类公共聚合必须自建。

**配额策略（重要）**：`discover` 按**源**分配额并**交错**输出，不做全局排序。
因为不同源的 score 量纲不可比（抖音千万级 vs GitHub star 千级 vs 无分值），
且「用抓取时刻当时间」的源会永远排在最前 —— 实测头条 50 条吃满 limit=40，
微博/知乎/B 站一条不剩。同理 NewsNow 的每个榜单注册成**独立源**，
否则一个源内部的「更新时间最新榜单」会把配额吃光。

**耗时**：11 源串行抓取约 20–25s（未并发）。

## 已知边界

- GitHub Trending 是 HTML 抓取：页面改版就取不到。失败表现为「该源今天没数据」
  （进 `errors[]`），不抛异常。
- HF 论文的时间取 `submittedOnDailyAt`（上今日榜的时刻）而非论文 `publishedAt`
  —— 后者常是几天前，按「最近 48 小时」过滤会把整个源过滤光。
- 没有中文 AI 资讯源（机器之心 RSS 302、InfoQ 451）。要补的话优先找**有官方
  RSS** 的源，不要再去试热搜镜像。

## 测试

```bash
pytest -q      # 18 例，离线（固定件 + 假抓取），不打网络
ruff check . && mypy src tests
```

## License

AGPL-3.0-or-later（与 STEPWORK 一致）。
