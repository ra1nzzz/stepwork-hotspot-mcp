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

## 数据源：2026-09-09 实测结论

| 源 | 结果 | 备注 |
|---|---|---|
| Hugging Face Daily Papers | ✅ | 免密钥；AI 论文，英文 |
| GitHub Trending | ✅ | 免密钥；抓 HTML，结构会变 |
| RSS/Atom（默认少数派） | ✅ | 免密钥；中文，偏效率工具/数码 |
| arXiv API | ❌ | 本机连不通（http 301 → https 仍 000） |
| RSSHub 公共实例 | ❌ | 403 反爬 |
| 36氪 `/feed` | ❌ | 返回 HTML，不是真 RSS |
| InfoQ `/feed` | ❌ | 451 |
| 微博热搜第三方镜像 | ❌ | 域名不可达 |

**结论：中文「社会热搜」这条路线在这台机器上拿不到**（无官方 RSS + 反爬 +
镜像不可达）。能稳定拿到的是「技术圈正在讨论什么」。这决定了上游产品形态：
不是追社会热点，是追技术动态。

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
