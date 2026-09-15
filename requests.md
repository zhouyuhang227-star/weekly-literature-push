项目规划与代码骨架，技术路线为：OpenAlex API（按 ISSN 过滤顶刊 + 关键词检索）→ OpenAI 格式接口做相关性匹配 → GitHub Actions 每周三 23:00（UTC 15:00）定时触发 → 结果邮件推送 → JSON 文件持久化 DOI 去重。
一、项目概览
维度	选型
文献源	OpenAlex https://api.openalex.org/works
期刊限定	用 primary_location.source.issn 或 host_venue.issn 过滤多个 ISSN
AI 匹配	任意兼容 OpenAI 格式的端点（base_url + api_key + model 三个参数即可切换）
定时	GitHub Actions schedule.cron（Actions 使用 UTC 时区，北京时间周三 23:00 = UTC 周三 15:00，即 0 15 * * 3） bradgarropy.com
邮件	Python 标准库 smtplib + email.mime，通过 SMTP 发送
去重	data/pushed_dois.json 由 AI 自动 commit 回仓库（需 contents: write 权限）
日志	每次运行写一份 data/logs/YYYY-MM-DD.log，同样 commit 回仓库

注：GitHub 于 2026 年 3 月起支持在 cron 上写 timezone 字段，可改用 timezone: Asia/Shanghai + 0 23 * * 3 的写法；若仓库 runner 未生效，请回退到 UTC 写法。
二、顶刊 ISSN 对照表（唯一事实来源，配置在 src/config.py）
代码中通过 issn:xxx|yyy 的管道语法实现多刊 OR 过滤。
期刊全名	缩写	eISSN（推荐，电子版）	pISSN	出版商
Nature	Nat	1476-4687	0028-0836	Springer Nature
Science	Sci	1095-9203	0036-8075	AAAS
Joule	Joule	2542-4351	—	Cell Press
Angewandte Chemie Int. Ed.	Angew	1521-3773	0044-8249	Wiley
Advanced Materials	AM	1521-4095	0935-9648	Wiley
Energy & Environmental Science	EES	1754-5706	1754-5692	RSC
Nature Energy	Nat Energy	2058-7546	—	Springer Nature
Nature Chemistry	Nat Chem	1755-4349	1755-4330	Springer Nature
Nature Sustainability	Nat Sustain	2398-9629	—	Springer Nature
Nature Materials	Nat Mater	1476-4660	1476-1122	Springer Nature
Advanced Functional Materials	AFM	1616-3028	1616-301X	Wiley

把 ISSN 集中在 src/config.py 的一个字典里，想增删期刊只改这一处。
三、项目目录结构
battery-literature-bot/
├── .github/
│   └── workflows/
│       └── weekly_push.yml          # 定时调度
├── src/
│   ├── __init__.py
│   ├── config.py                    # 期刊 ISSN、关键词、模型参数等常量
│   ├── openalex_client.py           # OpenAlex 检索 + 摘要还原
│   ├── ai_matcher.py                # OpenAI 格式调用，相关性打分
│   ├── dedup.py                     # DOI 去重（读写 JSON）
│   ├── mailer.py                    # SMTP 邮件
│   ├── logger.py                    # 简单封装 logging
│   └── main.py                      # 主流程编排
├── data/
│   ├── pushed_dois.json             # 已推送 DOI 持久化
│   └── logs/                        # 每周运行日志
├── requirements.txt
└── README.md
改邮件就只改 mailer.py，换 AI 供应商就只改 ai_matcher.py，换期刊就只改 config.py。
四、核心代码骨架
1. src/config.py
"""全局配置：所有"可能要改"的东西都集中在这里。"""import os
# ---- 顶刊 ISSN（eISSN 优先，缺 eISSN 用 pISSN）----
JOURNALS: dict[str, str] = {
    "Nature":            "1476-4687",
    "Science":           "1095-9203",
    "Joule":             "2542-4351",
    "Angew":             "1521-3773",
    "AM":                "1521-4095",
    "EES":               "1754-5706",
    "Nature Energy":     "2058-7546",
    "Nature Chemistry":  "1755-4349",
    "Nature Sustainability": "2398-9629",
    "Nature Materials":  "1476-4660",
    "AFM":               "1616-3028",
}# OpenAlex 的 OR 语法：用 | 拼接
ISSN_FILTER = "|".join(JOURNALS.values())
# ---- 用户关键词（可改成从环境变量/文件读取）----
USER_KEYWORDS: list[str] = ["solid-state battery", "lithium metal anode"]
# ---- OpenAI 兼容端点 ----
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_API_KEY  = os.getenv("AI_API_KEY", "")
AI_MODEL    = os.getenv("AI_MODEL", "gpt-4o-mini")
# ---- 邮件 ----
SMTP_HOST   = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")   # 用授权码，不是登录密码
MAIL_TO     = os.getenv("MAIL_TO", "")
# ---- 其他 ----
PER_PAGE        = 50
MAX_WORKS_FETCH = 200        # 每次最多拉多少条原始候选
DATA_DIR        = "data"
PUSHED_FILE     = f"{DATA_DIR}/pushed_dois.json"
2. src/openalex_client.py
"""OpenAlex 检索客户端。只关心"给关键词，返回结构化文献列表"。"""import requestsfrom .config import ISSN_FILTER, PER_PAGE, MAX_WORKS_FETCH

BASE = "https://api.openalex.org/works"
MAILTO = "you@example.com"   # OpenAlex 礼貌池要求带 mailto
def fetch_works(keywords: list[str]) -> list[dict]:
    """对每个关键词发起一次检索，合并去重后返回候选列表。"""
    results, seen = [], set()
    for kw in keywords:
        params = {
            "search": kw,
            "filter": f"primary_location.source.issn:{ISSN_FILTER}",
            "per-page": PER_PAGE,
            "mailto": MAILTO,
        }
        resp = requests.get(BASE, params=params, timeout=30)
        resp.raise_for_status()
        for w in resp.json().get("results", []):
            if w["id"] in seen:
                continue
            seen.add(w["id"])
            results.append(parse_work(w))
            if len(results) >= MAX_WORKS_FETCH:
                return results
    return results
def parse_work(w: dict) -> dict:
    return {
        "doi":        (w.get("doi") or "").replace("https://doi.org/" , ""),
        "title":      w.get("display_name", ""),
        "journal":    ((w.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
        "pub_date":   w.get("publication_date", ""),
        "cited_by":   w.get("cited_by_count", 0),
        "url":        w.get("doi") or w.get("id"),
        "abstract":   reconstruct_abstract(w.get("abstract_inverted_index")),
    }
def reconstruct_abstract(inv: dict | None) -> str:
    """OpenAlex 用倒排索引存摘要，需还原成明文<span data-allow-html class='source-item source-aggregated' data-group-key='source-group-4' data-url='https://github&#46;com/ourresearch/openalex&#45;docs/blob/main/api&#45;entities/works/work&#45;object/README&#46;md' data-id='turn0search12'><span data-allow-html class='source-item-num' data-group-key='source-group-4' data-id='turn0search12' data-url='https://github&#46;com/ourresearch/openalex&#45;docs/blob/main/api&#45;entities/works/work&#45;object/README&#46;md'><span class='source-item-num-name' data-allow-html>github.com</span><span data-allow-html class='source-item-num-count'></span></span></span>。"""
    if not inv:
        return ""
    pos2word = {}
    for word, positions in inv.items():
        for p in positions:
            pos2word[p] = word
    return " ".join(pos2word[i] for i in sorted(pos2word))
3. src/ai_matcher.py
"""用 OpenAI 格式接口对每篇文献做相关性判断。换供应商只改 BASE_URL/KEY/MODEL。"""import jsonimport requestsfrom .config import AI_BASE_URL, AI_API_KEY, AI_MODEL, USER_KEYWORDS

PROMPT = """你是电池领域的文献筛选助手。根据用户关键词判断以下论文是否高度相关。
用户关键词：{kw}
论文标题：{title}
摘要：{abstract}
只输出 JSON：{{"relevant": true/false, "score": 0-100, "reason": "一句话中文理由"}}
"""
def filter_relevant(works: list[dict], threshold: int = 60) -> list[dict]:
    out = []
    for w in works:
        if not w["abstract"] and not w["title"]:
            continue
        try:
            r = requests.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={
                    "model": AI_MODEL,
                    "messages": [{"role": "user", "content": PROMPT.format(
                        kw=", ".join(USER_KEYWORDS),
                        title=w["title"],
                        abstract=w["abstract"][:3000],
                    )}],
                    "temperature": 0,
                },
                timeout=60,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content[content.find("{"): content.rfind("}") + 1])
            if data.get("relevant") and int(data.get("score", 0)) >= threshold:
                w["ai_score"] = data["score"]
                w["ai_reason"] = data.get("reason", "")
                out.append(w)
        except Exception as e:
            print(f"[ai_matcher] skip {w.get('doi')}: {e}")
    return sorted(out, key=lambda x: -x["ai_score"])
4. src/dedup.py
"""DOI 去重：JSON 文件即数据库，简单、可 diff、可回滚。"""import json, osfrom .config import PUSHED_FILE
def load() -> set[str]:
    if not os.path.exists(PUSHED_FILE):
        return set()
    with open(PUSHED_FILE, encoding="utf-8") as f:
        return set(json.load(f).get("dois", []))
def save(new_dois: set[str]) -> None:
    os.makedirs(os.path.dirname(PUSHED_FILE), exist_ok=True)
    merged = {"dois": sorted(load() | new_dois)}
    with open(PUSHED_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
def drop(pushed: set[str], works: list[dict]) -> list[dict]:
    return [w for w in works if w["doi"] and w["doi"] not in pushed]
5. src/mailer.py
"""SMTP 邮件。QQ/163 邮箱记得用授权码；Gmail 需开 App Password。"""import smtplibfrom email.mime.multipart import MIMEMultipartfrom email.mime.text import MIMETextfrom .config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO
def send(works: list[dict], run_date: str) -> None:
    if not works:
        return
    html = f"<h2>本周电池顶刊文献推送 · {run_date}</h2>"
    for w in works:
        html += f"""
        <p><b>[{w['journal']}] {w['pub_date']}</b><br>
        <a href="{w['url']}">{w['title']}</a><br>
        AI评分: {w.get('ai_score')} — {w.get('ai_reason','')}<br>
        DOI: {w['doi']} | 被引: {w['cited_by']}</p><hr>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"电池顶刊周报 · {run_date}（{len(works)}篇）"
    msg["From"], msg["To"] = SMTP_USER, MAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
6. src/main.py
"""主流程：检索 → AI 匹配 → 去重 → 发邮件 → 落盘。"""import logging, sysfrom datetime import date, timedelta, datetimefrom zoneinfo import ZoneInfofrom . import openalex_client, ai_matcher, dedup, mailerfrom .config import USER_KEYWORDS, DATA_DIRfrom .logger import get_logger

log = get_logger("main")
def run(keywords: list[str] = None) -> None:
    keywords = keywords or USER_KEYWORDS
    run_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    log.info(f"=== Run {run_date}, keywords={keywords} ===")

    # 1. 拉取候选
    works = openalex_client.fetch_works(keywords)
    log.info(f"fetched {len(works)} candidate works")

    # 2. 去重（在 AI 之前做，省 token）
    pushed = dedup.load()
    works = dedup.drop(pushed, works)
    log.info(f"{len(works)} works after dedup")

    # 3. AI 相关性过滤
    relevant = ai_matcher.filter_relevant(works)
    log.info(f"{len(relevant)} works after AI filtering")

    # 4. 发邮件
    try:
        mailer.send(relevant, run_date)
        log.info("email sent")
        dedup.save({w["doi"] for w in relevant})   # 只有发送成功才写入
    except Exception as e:
        log.error(f"mail failed: {e}")
        raise   # 让 Actions 标红，便于排查
if __name__ == "__main__":
    run()
src/logger.py：
import logging, osfrom datetime import datetimefrom .config import DATA_DIR
def get_logger(name: str) -> logging.Logger:
    os.makedirs(f"{DATA_DIR}/logs", exist_ok=True)
    fname = f"{DATA_DIR}/logs/{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.FileHandler(fname, encoding="utf-8"), logging.StreamHandler()],
    )
    return logging.getLogger(name)
五、GitHub Actions 工作流
.github/workflows/weekly_push.yml
name: weekly-literature-push
on:
  schedule:
    - cron: "0 15 * * 3"          # UTC 周三 15:00 = 北京时间周三 23:00<span data-allow-html class='source-item source-aggregated' data-group-key='source-group-5' data-url='https://bradgarropy&#46;com' data-id='turn0search4'><span data-allow-html class='source-item-num' data-group-key='source-group-5' data-id='turn0search4' data-url='https://bradgarropy&#46;com'><span class='source-item-num-name' data-allow-html>bradgarropy.com</span><span data-allow-html class='source-item-num-count'></span></span></span>
  workflow_dispatch:               # 支持手动触发，调试用
permissions:
  contents: write                  # 允许 commit pushed_dois.json 和日志
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run pipeline
        env:
          AI_BASE_URL: ${{ secrets.AI_BASE_URL }}
          AI_API_KEY:  ${{ secrets.AI_API_KEY }}
          AI_MODEL:    ${{ secrets.AI_MODEL }}
          SMTP_HOST:   ${{ secrets.SMTP_HOST }}
          SMTP_PORT:   ${{ secrets.SMTP_PORT }}
          SMTP_USER:   ${{ secrets.SMTP_USER }}
          SMTP_PASS:   ${{ secrets.SMTP_PASS }}
          MAIL_TO:     ${{ secrets.MAIL_TO }}
        run: python -m src.main

      - name: Commit dedup & logs
        run: |
          git config user.name  "github-actions[bot]"
          config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/
          git diff --cached --quiet || (git commit -m "chore: update pushed DOIs & logs $(date +%F)" && git push)
requirements.txt：
requests>=2.31
整个项目刻意只依赖 requests，其余全部用标准库（smtplib/email/logging/json/zoneinfo），降低 Actions 环境踩坑概率。若想用 openai SDK 替代 requests 直调，把 ai_matcher.py 改一下即可，接口边界已经隔离好。
六、GitHub Secrets 清单（部署时一次性配置）
在仓库 Settings → Secrets and variables → Actions 中添加：
Secret	说明
AI_BASE_URL	例如 https://api.openai.com/v1，或任何兼容端点
AI_API_KEY	对应的 key
AI_MODEL	例如 gpt-4o-mini
SMTP_HOST	如 smtp.gmail.com / smtp.qq.com
SMTP_PORT	SSL 端口通常 465
SMTP_USER	发件邮箱
SMTP_PASS	授权码 / App Password（不是登录密码）
MAIL_TO	收件邮箱

安全约束：严禁把任何 key 写进代码或 config.py 默认值，一律走环境变量；workflow 中用 ${{ secrets.X }} 注入。
七、部署步骤（给下一个 AI 的执行清单）
1.新建 GitHub 仓库，按上述目录结构落盘所有文件。
2.在仓库 Secrets 中填入上表 8 个变量。
3.首次手动 workflow_dispatch 触发一次，查看 Actions 日志确认：OpenAlex 返回非 0 条 → AI 过滤无报错 → SMTP 发送成功。
4.检查 data/pushed_dois.json 是否被 bot commit；下次运行时重复 DOI 应被跳过。
5.关键词变更：改 src/config.py 中的 USER_KEYWORDS（或重构为从 data/keywords.txt 读取，由 bot 每次 commit 用户提交的关键词，属可选增强）。
6.增删期刊：只改 src/config.py 的 JOURNALS 字典。
八、已知的坑 & 给下一个 AI 的注意事项
时区：Actions 的 schedule 按 UTC 解释 cron，23:00 北京时间必须写成 0 15 * * 3；2026 年 3 月后可尝试 timezone: Asia/Shanghai 的新写法。
摘要为空：OpenAlex 用 abstract_inverted_index 倒排索引存摘要，必须用 reconstruct_abstract 还原，否则 AI 匹配会缺输入。
过滤字段：host_venue 已被官方标记 deprecated，新代码统一用 primary_location.source.issn。
AI 输出解析：LLM 可能返回带 ```json 包裹或额外文字，代码里用首尾 {/} 截取，不要直接 json.loads(全串)。
去重时机：在 AI 调用之前去重，能省大量 token；只有邮件发送成功才把 DOI 写回，避免"AI 判了但邮件挂了"导致文献永久丢失。
日志持久化：直接把 data/logs/ commit 回仓库最简单；如果不想污染 git 历史，可改用 actions/upload-artifact，但保留 90 天限制。
OpenAlex 礼貌池：请求带 mailto 参数可进入 faster poolo，openalex_client.py 已内置。
手动触发：workflow_dispatch 一定要保留，否则调试时只能干等周三。
把这份文档连同上面的代码骨架一起交给下一个 AI，它只需要：① 落盘文件 ② 填 Secrets ③ 跑一次手动触发验证 ④ 按需调整关键词/期刊，即可上线。