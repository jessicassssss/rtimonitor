#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RTI Schedule Monitor — Pure Standard Library Edition
- 只用 Python 標準函式庫，無需 pip
- 「快訊」只認 /programnews?pid=... 的頁面（不掃 PDF）
- 三條蒐集路徑（依序）：
  A) 節目表直接有單集：/programnews?pid=... → 直接檢查
  B) 節目表有節目首頁：/program?pid=... → 進節目頁找當日單集
  C) 若 A/B 都沒有，但節目表 HTML 內含 mp3 → 以 mp3 檔名回推 pid → 再回節目頁找當日單集；若仍找不到，視為無快訊
- 產出 TSV(預設) 或 CSV（由 config 設定 delimiter）
- 檔案用 UTF-8-SIG（Excel 友善），欄位內會移除換行與多餘空白
用法：
  python monitor_stdlib.py config-stdlib.json --date 2025-09-01
"""

import sys, os, csv, re, json
from datetime import datetime, date
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from typing import Dict, Any, List, Tuple

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------- 基礎工具 -----------------------

def fetch_text(url: str, timeout: int = 25) -> str:
    req = Request(url, headers=DEFAULT_HEADERS, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        code = resp.getcode()
        if 200 <= code < 300:
            return resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {code}")

def fetch_size_bytes(url: str, timeout: int = 20) -> int:
    # 盡量不下載整檔：先試 Range，再試一般 GET 讀 Content-Length
    headers = DEFAULT_HEADERS.copy()
    headers["Range"] = "bytes=0-0"
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            cr = resp.headers.get("Content-Range")
            if cr:
                m = re.search(r"/(\d+)\s*$", cr)
                if m: return int(m.group(1))
            cl = resp.headers.get("Content-Length")
            if cl: return int(cl)
    except Exception:
        pass
    try:
        req = Request(url, headers=DEFAULT_HEADERS, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            if cl: return int(cl)
    except Exception:
        pass
    return 0

def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()

def sanitize_cell(x):
    """避免 Excel 亂行：去掉換行、規一空白；保留中文與常見符號。"""
    if x is None: return ""
    s = str(x).replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s

def base_of(url: str) -> str:
    m = re.match(r"^https?://[^/]+", url)
    return m.group(0) if m else ""

def absolutize(base: str, href: str) -> str:
    if href.startswith("//"): return "https:" + href
    if re.match(r"^https?://", href): return href
    if href.startswith("/"): return base + href
    return base.rstrip("/") + "/" + href.lstrip("./")

def date_variants(d: date) -> List[str]:
    months_short = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    months_long  = ["January","February","March","April","May","June","July","August","September","October","November","December"]
    y, m, dd = d.year, d.month, d.day
    return [
        f"{y:04d}-{m:02d}-{dd:02d}",
        f"{y:04d}/{m:02d}/{dd:02d}",
        f"{dd:02d}/{m:02d}/{y:04d}",
        f"{m:02d}/{dd:02d}/{y:04d}",
        f"{months_short[m-1]} {dd}, {y}",
        f"{months_long[m-1]} {dd}, {y}",
    ]

def norm_title(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s.lower()

def pid_from_audio_url(u: str) -> str | None:
    """
    從 RTI 的 mp3 檔名推回節目 pid。
    例：
      .../20250901_2200_0111_fr.mp3           -> 0111
      .../20250901_0000_1472_en.mp3           -> 1472
      .../krrti/files/202508/20250901_1830_0002_kr.mp3 -> 0002
    回傳去除前導零的 pid；抓不到回 None。
    """
    m = re.search(r'/(\d{8})_(\d{3,4})_(\d{3,6})_[a-z]{2}\.mp3$', u, re.I)
    if m:
        return m.group(3).lstrip("0") or "0"
    m = re.search(r'/(\d{8})_(\d{3,4})_(\d{3,6})\.mp3$', u, re.I)
    if m:
        return m.group(3).lstrip("0") or "0"
    return None

# ----------------------- 節目表解析 -----------------------

def find_links_from_schedule(html: str, base: str) -> List[Dict[str,str]]:
    """
    從節目表 HTML 找到：
      - program：/program?pid=...
      - episode：/programnews?pid=...
    回傳 list[{title,url,kind}]
    """
    out: List[Dict[str,str]] = []
    seen = set()

    def push(href: str, title_html: str, kind: str):
        url_abs = absolutize(base, href)
        if url_abs in seen: return
        seen.add(url_abs)
        out.append({"title": strip_tags(title_html), "url": url_abs, "kind": kind})

    # program
    for m in re.finditer(r"<a[^>]+href=(['\"])([^'\"]*?/program\?pid=[^'\"]+)\1[^>]*>(.*?)</a>", html, re.I|re.S):
        push(m.group(2), m.group(3), "program")
    # episode
    for m in re.finditer(r"<a[^>]+href=(['\"])([^'\"]*?/programnews\?pid=[^'\"]+)\1[^>]*>(.*?)</a>", html, re.I|re.S):
        push(m.group(2), m.group(3), "episode")

    return out

def find_audio_items_from_schedule(html: str, base: str) -> List[Dict[str,str]]:
    """
    從節目表頁面找音檔（不碰 PDF）：
    1) 抓 href/src 的 .mp3
    2) 抓純文字/JSON 的 https://...mp3
    回傳 list[{title, audio_url}]
    """
    def clean_title(s: str) -> str:
        s = re.sub(r"\bimg\b.*", "", s, flags=re.I)
        s = re.sub(r"\s{2,}", " ", s).strip()
        s = re.sub(r"[^A-Za-z0-9’'\-\.,:()!?\u00C0-\u024F\s]", " ", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s[:80]

    items: List[Dict[str,str]] = []
    seen = set()

    # 1) 屬性中的 .mp3
    for m in re.finditer(r'(?:src|href)\s*=\s*(["\'])([^"\']+\.mp3(?:\?[^"\']*)?)\1', html, re.I):
        audio_url = absolutize(base, m.group(2))
        if audio_url in seen: continue
        seen.add(audio_url)
        start = max(0, m.start() - 2000); end = min(len(html), m.end() + 2000)
        ctx_text = strip_tags(html[start:end])
        guess = ""
        for key in ["English Program","Beyond the Reefs","The Doomscroll News Report","News","ON AIR"]:
            if key.lower() in ctx_text.lower():
                guess = key; break
        if not guess: guess = clean_title(ctx_text)
        items.append({"title": guess, "audio_url": audio_url})

    # 2) 純文字中的 .mp3
    for u in re.findall(r'https?://[^\s"\'<>()]+\.mp3(?:\?[^\s"\'<>()]+)?', html, flags=re.I):
        audio_url = u
        if audio_url in seen: continue
        seen.add(audio_url)
        pos = html.find(u)
        start = max(0, pos - 2000); end = min(len(html), pos + len(u) + 2000)
        ctx_text = strip_tags(html[start:end])
        guess = ""
        for key in ["English Program","Beyond the Reefs","The Doomscroll News Report","News","ON AIR"]:
            if key.lower() in ctx_text.lower():
                guess = key; break
        if not guess: guess = clean_title(ctx_text)
        items.append({"title": guess, "audio_url": audio_url})

    print(f"[DEBUG] schedule audio total (attr+plain): {len(items)}")
    return items

def discover_programs_from_schedule(schedule_url: str) -> Tuple[List[Dict[str,str]], str, str]:
    html = fetch_text(schedule_url)
    base = base_of(schedule_url)
    items = find_links_from_schedule(html, base)
    return items, html, base

# ----------------------- 節目首頁找當日單集 -----------------------

def find_episode_for_date(program_url: str, d: date) -> Tuple[str,str] | Tuple[None,None]:
    try:
        html = fetch_text(program_url)
    except Exception as e:
        raise RuntimeError(f"program fetch failed: {e}")
    base = base_of(program_url)
    first: Tuple[str,str] = (None, None)  # type: ignore
    variants = date_variants(d)

    for m in re.finditer(r"<a[^>]+href=(['\"])([^'\"]*?/programnews\?pid=[^'\"]+)\1[^>]*>(.*?)</a>", html, re.I|re.S):
        href = absolutize(base, m.group(2))
        text = strip_tags(m.group(3))
        if first == (None, None):
            first = (text, href)  # type: ignore
        if any(v in text for v in variants):
            return (text, href)
        # 也在附近文本找日期
        ctx_start = max(0, m.start() - 200)
        ctx = strip_tags(html[ctx_start:m.end()+200])
        if any(v in ctx for v in variants):
            return (text, href)

    return first

# ----------------------- 單集檢查 -----------------------

def extract_audio_links(html: str, base: str) -> List[str]:
    out: set[str] = set()
    pattern = r'(?:src|href)\s*=\s*(["\'])([^"\']+\.(?:mp3|m4a|aac|wav|ogg)(?:\?[^"\']*)?)\1'
    for m in re.finditer(pattern, html, re.I):
        out.add(absolutize(base, m.group(2)))
    return list(out)

def check_episode(episode_url: str, audio_min_kb: int, bulletin_min_chars: int) -> Dict[str,Any]:
    try:
        html = fetch_text(episode_url)
    except Exception as e:
        return {
            "audio_ok": False, "bulletin_ok": False,
            "audio_urls": [], "audio_sizes_kb": [], "bulletin_len": 0,
            "issues": [f"episode fetch failed: {e}"]
        }

    base = base_of(episode_url)
    audio_urls = extract_audio_links(html, base)
    sizes_kb = [int(fetch_size_bytes(u) / 1024) for u in audio_urls]
    audio_ok = any(s >= audio_min_kb for s in sizes_kb)

    text = strip_tags(html)
    bulletin_len = len(text)
    bulletin_ok = bulletin_len >= bulletin_min_chars

    issues = []
    if not audio_ok: issues.append(f"No audio >= {audio_min_kb}KB")
    if not bulletin_ok: issues.append(f"Bulletin too short ({bulletin_len} chars)")

    return {
        "audio_ok": audio_ok,
        "bulletin_ok": bulletin_ok,
        "audio_urls": audio_urls,
        "audio_sizes_kb": sizes_kb,
        "bulletin_len": bulletin_len,
        "issues": issues
    }

# ----------------------- 主流程 -----------------------

def run(config_path: str, date_str: str | None = None, batch: str | None = None) -> None:
    cfg = json.load(open(config_path, encoding="utf-8"))
    overrides = cfg.get("overrides", {}) or {}
    bulletin_optional_set = {s.lower() for s in overrides.get("bulletin_optional_programs", [])}

    tgt = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else datetime.now().date()

    outdir = cfg.get("output_dir", "out")
    os.makedirs(outdir, exist_ok=True)

    defaults = cfg.get("defaults", {}) if cfg.get("defaults") else {}
    audio_min_kb_default = int(defaults.get("audio_min_kb", 2000))
    bulletin_min_chars_default = int(defaults.get("bulletin_min_chars", 40))

    languages = cfg.get("languages", [])
    if batch:
        languages = [l for l in languages if str(l.get("batch","")).upper() == batch.upper()]

    # 輸出分隔符：預設 Tab → 產生 .tsv；若改 "," → 產生 .csv
    csv_delim = cfg.get("csv_delimiter", "\t")

    rows: List[Dict[str,Any]] = []
    alerts: List[str] = []

    for lang in languages:
        code = lang["code"]
        display = lang.get("display_name", code)
        uid = str(lang.get("uid", "4"))
        audio_min_kb = int(lang.get("audio_min_kb", audio_min_kb_default))
        bulletin_min_chars = int(lang.get("bulletin_min_chars", bulletin_min_chars_default))

        date_slash = f"{tgt.year:04d}/{tgt.month:02d}/{tgt.day:02d}"
        schedule_url = f'{cfg["site_base"].rstrip("/")}/{code}/programschedule?uid={uid}&date={date_slash}'

        print(f"[DEBUG] Processing {display} | {schedule_url}")

        # 取節目表
        try:
            items, html, base = discover_programs_from_schedule(schedule_url)
        except Exception as e:
            alerts.append(f"[{display}] schedule fetch failed: {e}")
            try:
                html = fetch_text(schedule_url)
                base = base_of(schedule_url)
                items = []
            except Exception as e2:
                alerts.append(f"[{display}] schedule html fetch failed: {e2}")
                html, base, items = "", "", []

        print(f"[DEBUG] {display} schedule: program/episode links = {len(items)}")

        produced_any_for_lang = False

        # A/B 路徑
        for it in items:
            kind = it.get("kind", "")
            title = (it.get("title") or "").strip()
            url = it.get("url") or ""

            if kind == "episode" and "/programnews?pid=" in url:
                res = check_episode(url, audio_min_kb, bulletin_min_chars)
                status = "OK" if (res["audio_ok"] and res["bulletin_ok"]) else "ISSUE"
                rows.append({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": tgt.isoformat(), "lang_code": code, "lang": display,
                    "program_title": title, "program_url": schedule_url,
                    "episode_title": title, "episode_url": url,
                    "audio_ok": res["audio_ok"],
                    "audio_urls": ";".join(res["audio_urls"]),
                    "audio_sizes_kb": ";".join(str(x) for x in res["audio_sizes_kb"]),
                    "bulletin_len": res["bulletin_len"], "bulletin_ok": res["bulletin_ok"],
                    "status": status, "issues": "; ".join(res["issues"])
                })
                produced_any_for_lang = True
                continue

            if kind == "program" and "/program?pid=" in url:
                try:
                    ep_title, ep_url = find_episode_for_date(url, tgt)
                except Exception as e:
                    rows.append({
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "date": tgt.isoformat(), "lang_code": code, "lang": display,
                        "program_title": title, "program_url": url,
                        "episode_title": "", "episode_url": "",
                        "audio_ok": False, "audio_urls": "", "audio_sizes_kb": "",
                        "bulletin_len": 0, "bulletin_ok": False,
                        "status": "SCHEDULED-NO-EPISODE", "issues": f"find_episode error: {e}"
                    })
                    produced_any_for_lang = True
                    continue

                if not ep_url:
                    rows.append({
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "date": tgt.isoformat(), "lang_code": code, "lang": display,
                        "program_title": title, "program_url": url,
                        "episode_title": ep_title or "", "episode_url": "",
                        "audio_ok": False, "audio_urls": "", "audio_sizes_kb": "",
                        "bulletin_len": 0, "bulletin_ok": False,
                        "status": "SCHEDULED-NO-EPISODE", "issues": "No episode link for target date"
                    })
                    produced_any_for_lang = True
                    continue

                res = check_episode(ep_url, audio_min_kb, bulletin_min_chars)
                status = "OK" if (res["audio_ok"] and res["bulletin_ok"]) else "ISSUE"
                rows.append({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": tgt.isoformat(), "lang_code": code, "lang": display,
                    "program_title": title, "program_url": url,
                    "episode_title": ep_title or "", "episode_url": ep_url,
                    "audio_ok": res["audio_ok"],
                    "audio_urls": ";".join(res["audio_urls"]),
                    "audio_sizes_kb": ";".join(str(x) for x in res["audio_sizes_kb"]),
                    "bulletin_len": res["bulletin_len"], "bulletin_ok": res["bulletin_ok"],
                    "status": status, "issues": "; ".join(res["issues"])
                })
                produced_any_for_lang = True
                continue

        # C 路徑：A/B 無結果 → 從節目表抓 mp3，優先用 mp3 檔名回推 pid→program→當日 programnews
        if not produced_any_for_lang and html:
            audios = find_audio_items_from_schedule(html, base)
            print(f"[DEBUG] {display} schedule mp3 count: {len(audios)}")

            for a in audios:
                title = (a.get("title") or "").strip()
                audio_url = a.get("audio_url", "")

                size = fetch_size_bytes(audio_url)
                audio_ok_mp3 = (size >= audio_min_kb * 1024)

                used_episode = False
                res = None
                prog_url = ""
                ep_title = title
                ep_url = ""

                pid = pid_from_audio_url(audio_url)
                if pid:
                    prog_url = f'{cfg["site_base"].rstrip("/")}/{code}/program?uid={uid}&pid={pid}'
                    try:
                        ep_title2, ep_url2 = find_episode_for_date(prog_url, tgt)
                        if ep_url2:
                            ep_title, ep_url = ep_title2 or title, ep_url2
                            res = check_episode(ep_url, audio_min_kb, bulletin_min_chars)
                            used_episode = True
                    except Exception as e:
                        print(f"[DEBUG] pid {pid} fallback to raw mp3 for {title}: {e}")

                if used_episode and res is not None:
                    status = "OK" if (res["audio_ok"] and res["bulletin_ok"]) else "ISSUE"
                    rows.append({
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "date": tgt.isoformat(), "lang_code": code, "lang": display,
                        "program_title": title, "program_url": prog_url or schedule_url,
                        "episode_title": ep_title or "", "episode_url": ep_url,
                        "audio_ok": res["audio_ok"],
                        "audio_urls": ";".join(res["audio_urls"]),
                        "audio_sizes_kb": ";".join(str(x) for x in res["audio_sizes_kb"]),
                        "bulletin_len": res["bulletin_len"], "bulletin_ok": res["bulletin_ok"],
                        "status": status, "issues": "; ".join(res["issues"])
                    })
                    produced_any_for_lang = True
                else:
                    # 仍只剩 mp3：不視為有快訊（除非在 optional 清單）
                    bulletin_ok = norm_title(title) in bulletin_optional_set
                    issues_note = ("bulletin optional for this program"
                                   if bulletin_ok else "No programnews found for this program on schedule")
                    rows.append({
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "date": tgt.isoformat(), "lang_code": code, "lang": display,
                        "program_title": title, "program_url": schedule_url,
                        "episode_title": title, "episode_url": audio_url,
                        "audio_ok": audio_ok_mp3,
                        "audio_urls": audio_url,
                        "audio_sizes_kb": str(int(size/1024)) if size else "",
                        "bulletin_len": 0, "bulletin_ok": bulletin_ok,
                        "status": "OK" if (audio_ok_mp3 and bulletin_ok) else "ISSUE",
                        "issues": issues_note
                    })
                    produced_any_for_lang = True

        # 若三條路徑仍無任何列，至少輸出一列 NO-DATA
        if not produced_any_for_lang:
            rows.append({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "date": tgt.isoformat(), "lang_code": code, "lang": display,
                "program_title": "", "program_url": schedule_url,
                "episode_title": "", "episode_url": "",
                "audio_ok": "", "audio_urls": "", "audio_sizes_kb": "",
                "bulletin_len": "", "bulletin_ok": "",
                "status": "NO-DATA",
                "issues": "No program/episode/mp3 found from schedule"
            })

    # ---- 寫檔 ----
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_csv = os.path.join(outdir, f"schedule-report-{ts}.csv")
    out_json = os.path.join(outdir, f"schedule-report-{ts}.json")
    fields = ["timestamp","date","lang","program_title","program_url","episode_title","episode_url",
              "audio_ok","audio_urls","audio_sizes_kb","bulletin_len","bulletin_ok","status","issues"]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=",")  # 強制使用逗號
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    # 主控台摘要
    for r in rows:
        print("[{status}] {lang} | {program_title} | audio_ok={audio_ok} | bulletin_len={bulletin_len}".format(**{**{k:"" for k in fields}, **r}))
    print(f"Saved: {out_csv} and {out_json}")
    if alerts:
        print("Alerts:")
        for a in alerts:
            print(" - " + a)


# ----------------------- 入口 -----------------------

if __name__ == "__main__":
    cfg = "config-stdlib.json"
    date_str = None
    batch = None
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--config" and i+1 < len(argv):
            cfg = argv[i+1]; i += 2; continue
        if argv[i] == "--date" and i+1 < len(argv):
            date_str = argv[i+1]; i += 2; continue
        if argv[i] == "--batch" and i+1 < len(argv):
            batch = argv[i+1]; i += 2; continue
        i += 1
    run(cfg, date_str=date_str, batch=batch)

{
  "site_base": "https://www.rti.org.tw",
  "output_dir": "out",
  "defaults": {
    "audio_min_kb": 2000,
    "bulletin_min_chars": 40
  },
  "languages": [
    {
      "code": "en",
      "display_name": "English",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "fr",
      "display_name": "Français",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "es",
      "display_name": "Español",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "de",
      "display_name": "Deutsch",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "ru",
      "display_name": "Русский",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "ja",
      "display_name": "日本語",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "id",
      "display_name": "Bahasa Indonesia",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "th",
      "display_name": "ไทย",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "vn",
      "display_name": "Tiếng Việt",
      "uid": "4",
      "batch": "A"
    },
    {
      "code": "kr",
      "display_name": "한국어",
      "uid": "4",
      "batch": "A"
    }
  ],
  "overrides": {
    "bulletin_optional_programs": [
      "News",
      "ON AIR"
    ],
    "program_map": {
      "en": {
        "English Program": "1414",
        "Beyond the Reefs": "1472",
        "The Doomscroll News Report": "1477",
        "News": "1426",
        "新聞(10') News": "1426",
        "10') News": "1426",
        "聽見臺灣(15') Hear in Taiwan": "0560",
        "15') Hear in Taiwan": "0560",
        "臺灣出發吧(13') Let’s Go": "1450",
        "13') Let’s Go": "1450",
        "築夢福爾摩沙(15') Formosa Dream Chasers": "1448",
        "15') Formosa Dream Chasers": "1448",
        "探尋分歧(20') The Divide": "1447",
        "20') The Divide": "1447",
        "點唱機之旅(15') Jukebox Journey": "1452",
        "15') Jukebox Journey": "1452",
        "偶想知道(13') Taiwanna Know": "1444",
        "13') Taiwanna Know": "1444",
        "島嶼樂章(20') Island Tunes": "1456",
        "20') Island Tunes": "1456",
        "在台灣我們說(15')In Taiwan We Speak": "1451",
        "15')In Taiwan We Speak": "1451",
        "動態更新(13') Status Update": "1289",
        "13') Status Update": "1289",
        "聲動台灣(28') Taiwan Grooves": "1453",
        "28') Taiwan Grooves": "1453",
        "時代故事(20') Tales of our Time": "1442",
        "20') Tales of our Time": "1442",
        "分享你所愛(25')Geek Out": "1437",
        "25')Geek Out": "1437",
        "新時代台灣 (20')Come Along": "1434",
        "20')Come Along": "1434",
        "輕鬆學臺語（5'）Taigi Made Easy": "1455",
        "Taigi Made Easy": "1455",
        "映像動機（25'）Cinematic Motif": "1454",
        "Cinematic Motif": "1454",
        "經典回顧(20')Retro Reels": "1464",
        "20')Retro Reels": "1464",
        "寶島動物群(20')Formosa Fauna": "1465",
        "20')Formosa Fauna": "1465",
        "千禧混音帶(15')Millennial Mixtapes": "1466",
        "15')Millennial Mixtapes": "1466",
        "怎麼玩台北(13')What's Up Taipei?": "1467",
        "13')What's Up Taipei?": "1467",
        "國民外礁(25')Beyond the Reefs": "1472",
        "25')Beyond the Reefs": "1472",
        "亞洲流行40(25')Asia Pop 40": "1473",
        "40(25')Asia Pop 40": "1473",
        "歷史一刻(5')HistoryPod": "1474",
        "5')HistoryPod": "1474",
        "我竟然不知道(18')Oh! I Didn't Know That": "1476",
        "18')Oh! I Didn't Know That": "1476",
        "新聞點播(28')The Doomscroll News Report": "1477",
        "28')The Doomscroll News Report": "1477",
        "台灣一週大事(13')This Week in TaiwanThis Week in Taiwan": "1478",
        "13')This Week in TaiwanThis Week in Taiwan": "1478"
      },
      "堿名": {
        "節目名稱": "節目編號"
      },
      "fr": {
        "民音古調(15') Partitions Orientales": "0497",
        "15') Partitions Orientales": "0497",
        "央廣會客室(非固定節目)(30'~) Au micro de RTI": "1557",
        "Au micro de RTI": "1557",
        "聽友信箱(15') Courrier des auditeurs": "0512",
        "15') Courrier des auditeurs": "0512",
        "特別報導(8'-30') Décryptage": "1477",
        "8'-30') D": "1477",
        "地緣政治解析(10') Géopolitique": "0504",
        "opolitique": "0504",
        "財經雜誌(10') Graine de Business": "1558",
        "10') Graine de Business": "1558",
        "新聞(10') Journal de l'actualité": "0111",
        "10') Journal de l'actualit": "0111",
        "故宮瑰寶(10') L’heure des musées": "0721",
        "10') L’heure des mus": "0721",
        "亞太週報(10') La revue Asie-Pacifique": "1459",
        "10') La revue Asie-Pacifique": "1459",
        "造城手札(12') La ville en pratiques": "1559",
        "12') La ville en pratiques": "1559",
        "一己之力(11')": "1560",
        "11')": "1560",
        "新聞眾議台(30') On en parle à Taiwan": "1478",
        "30') On en parle": "1478",
        "天天不斷聽(60') Programme du jour": "1480",
        "60') Programme du jour": "1480",
        "永續發展(12') Retour à la source": "0511",
        "12') Retour": "0511",
        "一週新聞(15') Revue de l'actualité": "1150",
        "15') Revue de l'actualit": "1150",
        "自然建築之樂(11') Les joies de l'éco-construction": "1560",
        "11') Les joies de l'": "1560",
        "講古(11') Contes à Rebours": "1236",
        "11') Contes": "1236",
        "多元的台灣(16') Taïwan dans toute sa diversité": "1579",
        "wan dans toute sa diversit": "1579",
        "短波天天不斷聽(30') Programme OC": "1581",
        "30') Programme OC": "1581",
        "女性出頭天(11') Femmes d’ombre, femmes de lumière": "1583",
        "11') Femmes d’ombre, femmes de lumi": "1583",
        "台法之間(10') RTF, relations Taïwan-France": "1580",
        "10') RTF, relations Ta": "1580",
        "響樂主義(11')Sous des cieux amplifiés": "1585",
        "11')Sous des cieux amplifi": "1585"
      },
      "es": {
        "天涯比臨(3-5') Acortando distancias": "1544",
        "3-5') Acortando distancias": "1544",
        "藝界人生(6') Artista en Taiwán": "0821",
        "6') Artista en Taiw": "0821",
        "中文之路(15') Chino mandarín básico": "1552",
        "15') Chino mandar": "1552",
        "Paty有約(10') Cita con Paty": "0484",
        "10') Cita con Paty": "0484",
        "成語大觀園(6') Cuentos y proverbios chinos": "0625",
        "6') Cuentos y proverbios chinos": "0625",
        "80年代後華語歌曲(6') Después de los 80": "1547",
        "s de los 80": "1547",
        "郵局不打烊(18') El cartero": "0473",
        "18') El cartero": "0473",
        "福爾摩沙(10') 24/7 Formosa 24/7": "1542",
        "10') 24/7 Formosa 24/7": "1542",
        "文化走廊(10') Galería cultural": "0481",
        "10') Galer": "0481",
        "新聞(帶狀)(12') Informativo": "0472",
        "12') Informativo": "0472",
        "咖啡時間(10') La hora del café": "0822",
        "10') La hora del caf": "0822",
        "四面八方(10') La isla hermosa": "1290",
        "10') La isla hermosa": "1290",
        "季節菜單(10') Menú de temporada": "1540",
        "de temporada": "1540",
        "流行歌曲(6') Música pop": "1549",
        "sica pop": "1549",
        "台語歌曲(6') Música taiwanesa": "1548",
        "sica taiwanesa": "1548",
        "自由廣場(10') Plaza pública": "1257",
        "10') Plaza p": "1257",
        "當天完整節目(60') Programación completa": "0086",
        "60') Programaci": "0086",
        "輕鬆看新聞(15') Taiwán curioso": "1481",
        "15') Taiw": "1481",
        "運動專家(10') Taiwán deportivo": "1340",
        "n deportivo": "1340",
        "台灣360°(10') Taiwán en 360°": "1333",
        "360°(10') Taiw": "1333",
        "台灣脈動(15') Taiwán en contexto": "1005",
        "n en contexto": "1005",
        "台灣傳出去(15')星期二 Taiwan por el mundo": "1551",
        "Taiwan por el mundo": "1551",
        "走進華語世界(5') Chino en pocos minutos": "1553",
        "5') Chino en pocos minutos": "1553",
        "深度談臺灣(20') Taiwán a fondo": "1554",
        "20') Taiw": "1554",
        "行銷有意思(15') Marketing ConSentido": "1555",
        "15') Marketing ConSentido": "1555",
        "一句話的力量(10')Frases para el mármol": "1556",
        "10')Frases para el m": "1556",
        "寶島任意門(15')De un lugar a otro": "1558",
        "15')De un lugar a otro": "1558",
        "觀望台灣(15') El mirador de Taiwán": "1004",
        "15') El mirador de Taiw": "1004",
        "台灣科技全方位(15')Más allá de los chips": "1560",
        "de los chips": "1560"
      },
      "de": {
        "細說台灣(10') Formosaik": "0802",
        "10') Formosaik": "0802",
        "歌曲排行榜(15') Hitparade": "0320",
        "15') Hitparade": "0320",
        "聽友專區(非節目) Horerinfos": "2001",
        "Horerinfos": "2001",
        "火線話題(20') Kaleidoskop": "0318",
        "20') Kaleidoskop": "0318",
        "台灣珍膳美(10') Kulinarisches Taiwan": "0325",
        "10') Kulinarisches Taiwan": "0325",
        "台灣之歌(20') Musik aus Taiwan": "0336",
        "20') Musik aus Taiwan": "0336",
        "新聞(10') Nachrichten": "0314",
        "10') Nachrichten": "0314",
        "寶島之旅(25') Reise durch Taiwan": "0323",
        "25') Reise durch Taiwan": "0323",
        "台灣速寫(10') Rund um die Insel": "0316",
        "10') Rund um die Insel": "0316",
        "焦點掃描(10') Schlagzeilen der Woche": "0726",
        "10') Schlagzeilen der Woche": "0726",
        "追蹤報導(10') Taiwan Monitor": "0324",
        "10') Taiwan Monitor": "0324",
        "總統大選(非節目) Wahlen 2020": "2010",
        "Wahlen 2020": "2010",
        "財經廣角鏡(10') Wirtschaftsmagazin": "2007",
        "10') Wirtschaftsmagazin": "2007",
        "周末雜誌(10')星期日 Wochenendmagazin": "0631",
        "Wochenendmagazin": "0631",
        "財經新聞(10')Business News": "0328",
        "10')Business News": "0328",
        "德語節目 Deutsches Programm (30')": "0070",
        "Deutsches Programm (30')": "0070",
        "淡水直播(60')": "0001",
        "60')": "0001",
        "台灣德語社區(10') Leben in Taiwan": "2017",
        "10') Leben in Taiwan": "2017",
        "台灣奇遇(10') Inselabenteuer(重播)": "2021",
        "10') Inselabenteuer(": "2021",
        "文化廣場(10') Kulturpanorama": "0332",
        "10') Kulturpanorama": "0332"
      },
      "ru": {
        "全球唐人街 (20') Всемирный чайнатаун": "0357",
        "20')": "0357",
        "RTI會客室 (20') Гостиная МРТ": "1262",
        "聽見台灣 (15') Тайвань говорит": "1330",
        "15')": "1330",
        "俄羅斯漢學歷史 (20') Китаеведение – устная история": "0684",
        "經濟新聞 (15') Новости экономики": "0347",
        "公佈欄 (非廣播節目) Объявления": "2000",
        "文化新聞 (20') Панорама культурной жизни": "0353",
        "寶島旅遊 (10') Радиопутешествие по Тайваню": "0354",
        "10')": "0354",
        "MIT台灣製造 (10') Сделано на Тайване": "1043",
        "生活天地 (20') Тайвань и тайваньцы": "0351",
        "大家學華語 (10') Учим китайский!": "0648",
        "一個小時的節目(整個節目60') Часовая программа передач": "1166",
        "60')": "1166",
        "歷史交流站(15') Перекрёстки истории": "2003",
        "樂活台灣(20') На лёгкой волне": "2004",
        "音樂世界(20') Вестник меломана": "2005",
        "新聞(8') Новости": "0339",
        "8')": "0339",
        "聽眾信箱(10') Почтовый ящик МРТ": "0341",
        "新聞- 二 (8') Новости (вт)": "1239",
        "新聞- 一 (8') Новости (пн)": "1238",
        "新聞- 五 (8') Новости (пт)": "1242",
        "新聞- 三 (8') Новости (ср)": "1240",
        "新聞- 四 (8') Новости (чт)": "1241",
        "國際台灣 (20') Тайвань в мировой политике": "2008",
        "今日台灣 (20') Тайвань сегодня": "2010",
        "細説紅樓夢(30')": "2012",
        "30')": "2012"
      },
      "jp": {
        "寶島新發現 (30')宝島再発見": "0382",
        "30')": "0382",
        "體壇風雲(10') スポーツオンライン": "0381",
        "10')": "0381",
        "週末好時光(20') GO GO台湾": "0380",
        "20') GO GO": "0380",
        "今日短訊(5') きょうのキーワード (週一、月曜日）": "1558",
        "5')": "1558",
        "今日短訊(5') きょうのキーワード (週二、火曜日）": "1559",
        "今日短訊(5') きょうのキーワード (週三、水曜日）": "1560",
        "今日短訊(5') きょうのキーワード (週四、木曜日）": "1561",
        "今日短訊(5') きょうのキーワード (週五、金曜日）": "1562",
        "烏龍茶小憩站(5') ウーロンブレーク (週四、木曜日）": "1320",
        "烏龍茶小憩站(5') ウーロンブレーク (週三、水曜日）": "1319",
        "烏龍茶小憩站(5') ウーロンブレーク (週二、火曜日）": "0365",
        "來信阿里阿多(30') お便りありがとう": "0379",
        "那魯灣時間(5') ナルワンアワー (週一、月曜日）": "1456",
        "那魯灣時間(5') ナルワンアワー (週五、金曜日）": "1457",
        "爸爸桑的「非常台灣」(30') 馬場克樹の「とっても台湾」 (週日、日曜日）": "1563",
        "音樂小站(30') ミュージックステーション": "0364",
        "台灣會客室(10') ようこそT-roomへ": "0369",
        "T-room": "0369",
        "台灣四方報(20') よもやま台湾": "1318",
        "20')": "1318",
        "台灣資訊站(30') 台湾お気楽レポート": "1557",
        "柔力台灣(10') 台湾ソフトパワー": "0367",
        "台灣小百科(10') 台湾ミニ百科": "0370",
        "台灣經濟最前線(20') 台湾経済最前線": "0694",
        "對外關係(20') 対外関係": "0371",
        "數字台灣(10') 数字の台湾": "0363",
        "文化台灣(10') 文化の台湾": "0378",
        "影音新聞(5') 映像ニュース": "1486",
        "新聞(10') ニュース (週一、月曜日）": "1124",
        "新聞(10') ニュース (週四、木曜日）": "1127",
        "新聞(10') ニュース (週三、水曜日）": "1126",
        "新聞(10') ニュース (週二、火曜日）": "1125",
        "新聞(10') ニュース (週五、金曜日）": "1128",
        "生活華語(10') 生活中国語": "0375",
        "節目 番組(60')": "0073",
        "60')": "0073",
        "觀光華語(10') 観光中国語": "0366",
        "時光之旅(10') タイムトリップ": "1564",
        "RtiFM午安台灣(30') こんにちは、台湾！": "1568",
        "RtiFM": "1568"
      },
      "id": {
        "一體兩面 (20')Dua Sisi": "1481",
        "20')Dua Sisi": "1481",
        "常識101(5') Tahukah Anda": "1480",
        "101(5') Tahukah Anda": "1480",
        "A套節目1 Acara Siaran Indonesia Program 1(60')": "1083",
        "1 Acara Siaran Indonesia Program 1(60')": "1083",
        "B套節目2 Acara Siaran Indonesia Program 2(60')": "1084",
        "2 Acara Siaran Indonesia Program 2(60')": "1084",
        "哈啦東尼(20') Ada Apa Dengan Tony": "0413",
        "20') Ada Apa Dengan Tony": "0413",
        "封面人物(15') Apa & Siapa": "0737",
        "15') Apa & Siapa": "0737",
        "生活國臺語 週五(10') Belajar Mandarin & Taiyu Jumat": "1347",
        "10') Belajar Mandarin & Taiyu Jumat": "1347",
        "生活國臺語 週四(10') Belajar Mandarin & Taiyu Kamis": "0412",
        "10') Belajar Mandarin & Taiyu Kamis": "0412",
        "生活國臺語 週三(10') Belajar Mandarin & Taiyu Rabu": "1346",
        "10') Belajar Mandarin & Taiyu Rabu": "1346",
        "生活國臺語 週二(10') Belajar Mandarin & Taiyu Selasa": "1345",
        "10') Belajar Mandarin & Taiyu Selasa": "1345",
        "音樂萬花筒興(20') Blitz Musik": "0741",
        "20') Blitz Musik": "0741",
        "童話故事(15') Dongeng Si Udin": "0385",
        "15') Dongeng Si Udin": "0385",
        "醫學小百科(15') Dunia Kesehatan": "0411",
        "15') Dunia Kesehatan": "0411",
        "畫廊 Galeri(非節目)": "1476",
        "Galeri(": "1476",
        "文化走廊(20') Galeri Budaya": "0398",
        "20') Galeri Budaya": "0398",
        "單車日記(15') Gowes": "0748",
        "15') Gowes": "0748",
        "勞工資訊(20') Info Kita (B)": "1341",
        "20') Info Kita (B)": "1341",
        "美食無國界(10') Jelajah Kuliner": "1475",
        "10') Jelajah Kuliner": "1475",
        "瑪麗亞週記 (15')Jurnal Maria星期一(a)": "0416",
        "15')Jurnal Maria": "0416",
        "年輕新世代(20') Kampus": "0400",
        "20') Kampus": "0400",
        "匯集短文 Kedai RTISI(非節目)": "1477",
        "Kedai RTISI(": "1477",
        "華語流行曲 (20')M POP": "1348",
        "20')M POP": "1348",
        "人技關係(10') Manusia & Teknologi": "0750",
        "10') Manusia & Teknologi": "0750",
        "Men’s Talk (15')": "0753",
        "時光機(15') Mesin Waktu": "0747",
        "15') Mesin Waktu": "0747",
        "國樂欣賞(15') Musika Klasik": "0392",
        "15') Musika Klasik": "0392",
        "RTISI俱樂部(15') Obrolan RTISI": "1463",
        "15') Obrolan RTISI": "1463",
        "名家觀點 (5')Perspektif": "0390",
        "5')Perspektif": "0390",
        "今日臺灣(15') Taiwan Dewasa Ini": "0744",
        "15') Taiwan Dewasa Ini": "0744",
        "空中交流道 (10')Temu Udara": "0732",
        "10')Temu Udara": "0732",
        "姐妹淘(20') Warna Warni Wanita": "0722",
        "20') Warna Warni Wanita": "0722",
        "新聞 週五 Warta Berita Jumat(15')": "1117",
        "Warta Berita Jumat(15')": "1117",
        "新聞 週四 A套 (15')Warta Berita Kamis (A)": "1116",
        "15')Warta Berita Kamis (A)": "1116",
        "新聞 週四 B套(15') Warta Berita Kamis (B)": "1114",
        "15') Warta Berita Kamis (B)": "1114",
        "新聞 週三(15') Warta Berita Rabu": "1113",
        "15') Warta Berita Rabu": "1113",
        "新聞 週六(15') Warta Berita Sabtu": "1119",
        "15') Warta Berita Sabtu": "1119",
        "新聞 週二(15') Warta Berita Selasa": "1111",
        "15') Warta Berita Selasa": "1111",
        "新聞 週一 (15')Warta Berita Senin": "0386",
        "15')Warta Berita Senin": "0386",
        "新冠肺炎宣導 Kebijakan COVID-19 di Taiwan(非節目)": "1482",
        "Kebijakan COVID-19 di Taiwan(": "1482",
        "趣特搜 (20')Lacak Hobby (12/16停播)": "0754",
        "20')Lacak Hobby (12/16": "0754",
        "僑生Podcast": "1483",
        "Podcast": "1483",
        "北回電台(60')": "0003",
        "60')": "0003",
        "漁業電台 (40')": "0002",
        "40')": "0002",
        "RtiFM Senin 青春好時光": "1484",
        "RtiFM Senin": "1484",
        "RtiFM Selasa 佳蘭迦南": "1485",
        "RtiFM Selasa": "1485",
        "RtiFM Rabu 人在他鄉": "1486",
        "RtiFM Rabu": "1486",
        "RtiFM Kamis 生活科技網": "1487",
        "RtiFM Kamis": "1487",
        "RtiFM Jumat 今天要新福": "1488",
        "RtiFM Jumat": "1488"
      },
      "ww": {
        "教育電台 (30')": "0001",
        "30')": "0001"
      },
      "th": {
        "泰語節目A套(60') ฟังรายการอาร์ทีไอ": "0075",
        "60')": "0075",
        "RTI 俱樂部(30') สโมสรผู้ฟัง": "0552",
        "30')": "0552",
        "活力臺灣(15') มิติใหม่ ไต้หวัน": "1007",
        "15')": "1007",
        "臺灣趣聞(30') อย่างนี้คุณจะว่ายังไง": "0533",
        "娛樂達康(30') บันเทิงดอทคอม": "0540",
        "透視臺灣人(15') ที่นี่ไต้หวัน": "0525",
        "臺灣廚園(15') เลาะรั้ว ครัวไต้หวัน": "0543",
        "台灣泰幸福(15') บันทึกชีวิตในไต้หวัน": "1305",
        "勞工法令百寶(15') ไขปัญหาแรงงาน": "0549",
        "外勞資訊站(15') ขุนพล แรงงานไทย": "0545",
        "財經短訊(15') ชีพจรเศรษฐกิจ": "0521",
        "臺灣 High tech(15')ไต้หวันไฮเทค": "0520",
        "High tech(15')": "0520",
        "健康小百科(10') สารานุกรมสุขภาพ": "0522",
        "10')": "0522",
        "體育世界(15') เจาะลึก กีฬาโลก": "0527",
        "臺灣泰好玩(15') อะไร อะไร ในไต้หวัน": "0541",
        "自由風(15') กระแสประชาธิปไตย": "0517",
        "文化精華(15') มองปัจจุบัน ย้อนอดีต": "0526",
        "華語教學(10') (初級) วิทยาลัยภาษาจีนทางอากาศ(พื้นฐาน)": "0542",
        "10') (": "0542",
        "華語教學(10') (中級) วิทยาลัยภาษาจีนทางอากาศ(กลาง)": "1224",
        "華語教學(10') (高級) วิทยาลัยภาษาจีนทางอากาศ(สูง)": "1225",
        "每日新聞焦點(15') (星期一) ข่าวประจำวัน (จันทร์)": "0516",
        "15') (": "0516",
        "每日新聞焦點(15') (星期二) ข่าวประจำวัน (อังคาร)": "1220",
        "每日新聞焦點(15') (星期三) ข่าวประจำวัน (พุธ)": "1221",
        "每日新聞焦點(15') (星期四) ข่าวประจำวัน (พฤหัสบดี)": "1222",
        "每日新聞焦點(15') (星期五) ข่าวประจำวัน (ศุกร์)": "1223",
        "泰語節目B套(60') ฟังรายการชุดเอเชียสัมพันธ์": "0095",
        "華語泰好學(15') ภาษาจีนพาเพลิน": "0532",
        "僑生Podcast": "1306",
        "Podcast": "1306",
        "RtiFM 泰喜歡臺灣": "0534",
        "RtiFM": "0534",
        "RtiFM 移民工天地": "1216",
        "RtiFM 臺灣這麼說": "1217",
        "RtiFM 臺灣風情": "1218",
        "RtiFM 臺灣活力旺": "1219"
      },
      "vn": {
        "音樂排行榜(15') Bảng xếp hạng âm nhạc": "2043",
        "15') Ba": "2043",
        "文化市集(15') Điểm hẹn văn hóa": "2037",
        "15')": "2037",
        "教育小百科(15') Góc giáo dục": "2029",
        "15') Go": "2029",
        "迷人的海島(15') Hải đảo đáng yêu": "2046",
        "15') H": "2046",
        "今日節目 (Live) Nghe chương trình tiếng Việt (A) (60')": "0467",
        "Live) Nghe ch": "0467",
        "今日節目 (Live) Nghe chương trình tiếng Việt (B) (60')": "2040",
        "遇見台灣(15') Nhịp sống Đài Loan": "0451",
        "15') Nh": "0451",
        "放眼台灣(15') Theo dòng thời sự": "0453",
        "15') Theo do": "0453",
        "華語輕鬆學(10') Tiếng Hoa cho mỗi ngày (Th.Ba) (a)": "0444",
        "y (Th.Ba) (a)": "0444",
        "華語輕鬆學(10') Tiếng Hoa cho mỗi ngày (Th.Ba) (b)": "0457",
        "y (Th.Ba) (b)": "0457",
        "華語輕鬆學(10') Tiếng Hoa cho mỗi ngày (Th.Hai)": "1440",
        "ng Hoa cho m": "1440",
        "華語輕鬆學(10') Tiếng Hoa cho mỗi ngày (Th.Năm)": "1189",
        "華語輕鬆學(10') Tiếng Hoa cho mỗi ngày (Th.Sáu)": "1195",
        "華語輕鬆學(10') Tiếng Hoa cho mỗi ngày (Th.Tư)": "1183",
        "新聞(15') Tin thời sự (Th.Ba) (a)": "1175",
        "15') Tin th": "1175",
        "新聞(15') Tin thời sự (Th.Ba) (b)": "1178",
        "新聞(15') Tin thời sự (Th.Hai)": "0440",
        "新聞(15') Tin thời sự (Th.Năm)": "1185",
        "新聞(15') Tin thời sự (Th.Sáu)": "1191",
        "新聞(15') Tin thời sự (Th.Tư)": "1179",
        "越南家鄉新聞(5') Tin Việt Nam (thứ 3 -b)": "2047",
        "5') Tin Vi": "2047",
        "臺越一家親(15') Cộng đồng người Việt tại Đài Loan": "0466",
        "15') C": "0466",
        "聽友時間(15') Giờ hẹn các bạn (Th.Ba) (b)": "0471",
        "n (Th.Ba) (b)": "0471",
        "空中交流網(30') Nhịp cầu giao lưu": "0439",
        "30') Nhi": "0439",
        "臺灣廣角鏡(15') Ố́ng kính rộng": "0447",
        "ng ki": "0447",
        "年輕新世代(15') Thế hệ trẻ Đài Loan": "0454",
        "15') Th": "0454",
        "臺灣全紀錄(10') Tìm hiểu Đài Loan": "0442",
        "10') Ti": "0442",
        "生活櫥窗(10') Tủ kính sinh hoạt (Th. Ba ) b": "1438",
        "t (Th. Ba ) b": "1438",
        "1001個女人的故事(15') 1001 câu chuyện của nàng": "2058",
        "15') 1001 c": "2058",
        "快樂星期六(30') Gặp nhau cuối tuần": "2059",
        "p nhau cu": "2059",
        "親親寶貝(15') Cùng con": "2061",
        "15') Cu": "2061",
        "古典音樂好好聽 (20') Thế giới âm nhạc cổ điển": "2062",
        "20') Th": "2062",
        "臺言潮語 (15') Bắt trend Đài Loan": "2063",
        "t trend": "2063",
        "僑生Podcast": "2064",
        "Podcast": "2064",
        "RtiFM法律知識庫 Pháp luật đời sống": "2071",
        "RtiFM": "2071",
        "RtiFM華語輕鬆學 Tiếng Hoa thật thú vị": "2072",
        "ng Hoa th": "2072",
        "RtiFM臺越那些事 Câu chuyện Đài Việt": "2068",
        "u chuy": "2068",
        "RtiFM台灣就業通 Tuổi trẻ lập nghiệp": "2067",
        "p nghi": "2067",
        "RtiFM健康生活網 Sống vui sống khỏe": "2069",
        "ng vui s": "2069",
        "RtiFM跟著RTI去遊行 Hôm nay mình đi đâu thế": "2070",
        "m nay mi": "2070"
      },
      "kr": {
        "特別節目(非節目) 2020년 중화민국 국경일": "0010",
        "2020": "0010",
        "臺灣. 朝鮮半島. 兩岸專題(13') 한인사회. 한반도 및 양안관계": "0002",
        "13')": "0002",
        "新聞(7') 간추린 뉴스": "0021",
        "7')": "0021",
        "休閒旅遊(非節目) 고고 타이완": "0004",
        "花樣臺灣(非節目) 꽃보다 타이완": "0015",
        "美食先遣隊(10') 랜선 미식회": "0019",
        "10')": "0019",
        "復刻臺灣(10') 레트로 타이완": "0013",
        "走讀文青(10') 문화 산책": "0016",
        "藝文社會(非節目) 문화 탐방": "0003",
        "生活娛樂體育(非節目)(5') 스포테인먼트": "0007",
        "5')": "0007",
        "臺北連線(30') 안녕하세요~ 청취자님": "0011",
        "30')": "0011",
        "歷史民俗(非節目) 역사 속으로": "0009",
        "影視娛樂(10') 연예계 소식": "0018",
        "政經社外專題(非節目) 오늘의 타이완": "0014",
        "綜合(30') 종합": "0001",
        "綜合新聞評論(7') 주간 시사평론": "0022",
        "脫口說華韓雙語(9') 중국어.한국어.토크토크": "0020",
        "9')": "0020",
        "政外產經(非節目) 타이완 정외. 경제산업": "0006",
        "雲端踏查(10') 포르모사 링크": "0017",
        "民俗技藝面面觀(10') 현대 속 전통기예": "0012",
        "福爾摩沙文學館(10') 포르모사 문학관": "0023",
        "原來如此(9') 아리송한 표현 해결사": "0024",
        "臺灣真趣味(10') 타이베이 토크": "0025",
        "音樂花園(10') 멜로디 가든": "0026",
        "臺灣新報(10')대만주간신보": "0028",
        "私房景點探險(10')랜드마크 원정대": "0029",
        "速寫臺北(10')어반 스케쳐스 타이베이": "0030",
        "MZ世代魔幻二人組(9')신박한 MZ세대 둘": "0031"
      },
      "ph": {
        "RtiFM 你知道嗎？ Alam nyo naba": "1436",
        "Alam nyo naba": "1436",
        "RtiFM 民以食為天 Kainan na": "1437",
        "Kainan na": "1437",
        "RtiFM 大家一起來學中文 Let’s learn Mandarin": "1438",
        "Let’s learn Mandarin": "1438",
        "RtiFM 勞動法規你我他 Mga Patakaran": "1439",
        "Mga Patakaran": "1439",
        "RtiFM 旅遊達人 Tara Mamasyal": "1440",
        "Tara Mamasyal": "1440",
        "RtiFM 走吧，跟著我們 Halina’t Maglibang": "1441",
        "Halina’t Maglibang": "1441",
        "RtiFM 遇見臺灣 Dagdag Kaalaman": "1442",
        "Dagdag Kaalaman": "1442"
      }
    }
  }
}
