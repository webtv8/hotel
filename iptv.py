#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 酒店频道采集器
====================
全自动流程:
  1. 从 iptv.cqshushu.com 抓取酒店 IP:Port 列表
  2. 识别平台类型 (智慧光迅 / 智能KUTV / 华视美达)
  3. 获取频道列表并拼接正确的流地址
  4. 按 省份地区_运营商.txt 分组输出

用法:
  python iptv_scraper.py                          # 默认全流程
  python iptv_scraper.py --max-pages 10           # 抓取前 10 页
  python iptv_scraper.py --input ips.json         # 从已有 IP 文件开始
  python iptv_scraper.py --workers 8              # 8 线程并发
  python iptv_scraper.py --output-dir my_channels # 自定义输出目录

平台流地址格式:
  智慧光迅 → http://ip:port/hls/{id}/index.m3u8
  智能KUTV → http://ip:port/tsfile/live/{id}.m3u8
  华视美达 → http://ip:port/newlive/live/hls/{id}/live.m3u8
"""

import argparse
import json
import os
import re
import sys
import time
import base64
import urllib3
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("[!] 缺少依赖: pip install beautifulsoup4 lxml")
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 常量
# ============================================================
PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古",
    "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南",
    "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]

OPERATORS = ["联通", "电信", "移动", "广电"]

DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 11; TV) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/98.0.4758.102 Safari/537.36"
)


# ============================================================
# 工具
# ============================================================
def log(tag, msg, level="info"):
    """统一日志输出"""
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {
        "info":  "[+]",
        "warn":  "[!]",
        "err":   "[-]",
        "step":  "[*]",
    }.get(level, "[*]")
    print(f"  {ts} {prefix} {tag} | {msg}")


def safe_get(url, headers, timeout=10, retries=3):
    """带重试的 GET 请求"""
    for i in range(retries):
        try:
            r = requests.get(
                url, headers=headers,
                timeout=timeout, verify=False
            )
            if r.status_code == 429:
                wait = (i + 1) * 10
                log("HTTP", f"429 限流, 等待 {wait}s", "warn")
                time.sleep(wait)
                continue
            return r
        except requests.exceptions.Timeout:
            time.sleep(2)
        except requests.exceptions.ConnectionError:
            time.sleep(2)
        except Exception as e:
            log("HTTP", f"请求异常: {e}", "err")
            time.sleep(2)
    return None


def parse_type_info(type_str):
    """从类型字段解析省份/城市/运营商"""
    province = ""
    city = ""
    operator = ""

    for op in OPERATORS:
        if op in type_str:
            operator = op
            break

    for prov in PROVINCES:
        if prov in type_str:
            province = prov
            break

    hotel_idx = type_str.find("酒店")
    if hotel_idx > 0:
        prefix = type_str[:hotel_idx]
        city_part = prefix
        for prov in PROVINCES:
            city_part = city_part.replace(prov, "")
        for suffix in ["省", "市", "自治区", "壮族", "回族", "维吾尔"]:
            city_part = city_part.replace(suffix, "")
        city_part = city_part.strip()
        if city_part:
            city = city_part

    return {"province": province, "city": city, "operator": operator}


def make_filename(province, city, operator):
    """生成分组文件名"""
    parts = []
    if province:
        parts.append(province)
    if city:
        parts.append(city)
    name = "".join(parts)
    if operator:
        name += f"_{operator}"
    return f"{name}.txt" if name else "未知.txt"


# ============================================================
# 第一阶段: 抓取酒店 IP:Port 列表
# ============================================================
class HotelIPScraper:
    """从 iptv.cqshushu.com 抓取酒店类型 IP"""

    BASE_URL = "https://iptv.cqshushu.com"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://iptv.cqshushu.com/",
        "Connection": "keep-alive",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False

    def _bypass_js(self):
        """绕过 JS 验证"""
        for attempt in range(3):
            try:
                r = self.session.get(
                    self.BASE_URL, headers=self.HEADERS, timeout=15
                )
                m = re.search(
                    r"var c='([a-f0-9]+)'\+'_'\+Date\.now\(\);", r.text
                )
                if not m:
                    if "Hotel IPTV" in r.text:
                        log("验证", "无需验证，可直接访问")
                        return True
                    continue

                key_prefix = m.group(1)
                ts = int(time.time() * 1000)
                self.session.cookies.set(
                    "list_js_verified",
                    f"{key_prefix}_{ts}",
                    domain="iptv.cqshushu.com",
                )

                fp = base64.b64encode(
                    json.dumps({
                        "ua": self.HEADERS["User-Agent"],
                        "lang": "zh-CN",
                        "platform": "Win32",
                        "screen": "1920x1080",
                        "timezone": "Asia/Shanghai",
                        "timestamp": ts,
                    }).encode()
                ).decode()
                self.session.cookies.set(
                    "browser_fp", fp, domain="iptv.cqshushu.com"
                )

                time.sleep(2)
                r2 = self.session.get(
                    f"{self.BASE_URL}/?_js=1",
                    headers=self.HEADERS, timeout=15,
                )
                if "Hotel IPTV" in r2.text or "Multicast IP" in r2.text:
                    log("验证", "JS 验证通过")
                    return True

            except Exception as e:
                log("验证", f"尝试 {attempt+1} 失败: {e}", "err")
                time.sleep(3)

        return False

    def _fetch_page(self, page):
        """抓取酒店列表的某一页"""
        url = f"{self.BASE_URL}/?t=hotel&page={page}&_js=1"
        r = safe_get(url, self.HEADERS, timeout=15)
        if not r or r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", class_="iptv-table")
        if not table:
            tables = soup.find_all("table")
            table = tables[0] if tables else None
        if not table:
            return []

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

        items = []
        for row in rows:
            cols = row.find_all("td")
            if len(cols) >= 6:
                status = cols[5].get_text().strip()
                if status != "新上线":
                    continue
                items.append({
                    "ip": cols[0].get_text().strip(),
                    "port": cols[1].get_text().strip(),
                    "ip_port": (
                        f"{cols[0].get_text().strip()}"
                        f":{cols[1].get_text().strip()}"
                    ),
                    "channels": cols[2].get_text().strip(),
                    "type": cols[3].get_text().strip(),
                    "online_time": cols[4].get_text().strip(),
                    "update_time": cols[5].get_text().strip(),
                    "status": status,
                })
            elif len(cols) >= 5:
                items.append({
                    "ip": cols[0].get_text().strip(),
                    "port": cols[1].get_text().strip(),
                    "ip_port": (
                        f"{cols[0].get_text().strip()}"
                        f":{cols[1].get_text().strip()}"
                    ),
                    "channels": cols[2].get_text().strip(),
                    "type": cols[3].get_text().strip(),
                    "online_time": cols[4].get_text().strip(),
                    "update_time": "",
                    "status": "",
                })

        return items

    def run(self, max_pages=10):
        """执行抓取，返回 IP 列表"""
        log("IP采集", "开始抓取酒店 IP 列表...", "step")

        if not self._bypass_js():
            log("IP采集", "JS 验证失败", "err")
            return []

        time.sleep(2)
        all_items = []

        for p in range(1, max_pages + 1):
            items = self._fetch_page(p)
            if not items:
                log("IP采集", f"第 {p} 页无数据，停止翻页")
                break
            all_items.extend(items)
            log("IP采集", f"第 {p} 页: {len(items)} 条, 累计 {len(all_items)}")
            time.sleep(3)

        log("IP采集", f"共获取 {len(all_items)} 个新上线 IP")
        return all_items


# ============================================================
# 第二阶段: 平台识别 + 频道抓取
# ============================================================
class ChannelScraper:
    """识别平台类型并抓取频道列表"""

    TIMEOUT = 10
    HEADERS = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    # ---------- 识别 ----------

    def identify(self, ip_port):
        """返回 (平台名, 平台标识字符串)"""
        base = f"http://{ip_port}"
        try:
            r = requests.get(
                base, headers=self.HEADERS,
                timeout=self.TIMEOUT, verify=False,
            )
            body = r.text

            if "ZHGXTV" in body:
                return "智慧光迅", "zhgxtv"
            if "/iptv/live/zh_cn.js" in body:
                return "智能KUTV", "kutv"
            if "华视美达" in body:
                return "华视美达", "huashi"

            return self._probe(ip_port)

        except requests.exceptions.Timeout:
            return "超时", None
        except requests.exceptions.ConnectionError:
            return "连接失败", None
        except Exception as e:
            return f"错误:{e}", None

    def _probe(self, ip_port):
        base = f"http://{ip_port}"
        probes = [
            ("/ZHGXTV/Public/json/live_interface.txt", "智慧光迅", "zhgxtv"),
            ("/iptv/live/zh_cn.js", "智能KUTV", "kutv"),
            ("/newlive/live/hls/1/live.m3u8", "华视美达", "huashi"),
        ]
        for path, name, tag in probes:
            try:
                r = requests.get(
                    f"{base}{path}",
                    headers=self.HEADERS, timeout=5, verify=False,
                )
                if r.status_code == 200 and len(r.text) > 10:
                    return name, tag
            except Exception:
                continue
        return "未识别", None

    # ---------- 频道抓取 ----------

    def fetch(self, ip_port, tag):
        """根据平台标识抓取频道"""
        if tag == "zhgxtv":
            return self._fetch_zhgxtv(ip_port)
        if tag == "kutv":
            return self._fetch_kutv(ip_port)
        if tag == "huashi":
            return self._fetch_huashi(ip_port)
        return []

    # --- 智慧光迅 ---
    def _fetch_zhgxtv(self, ip_port):
        """
        频道列表: /ZHGXTV/Public/json/live_interface.txt (JSON)
        流地址:   http://ip:port/hls/{channel_id}/index.m3u8
        """
        base = f"http://{ip_port}"
        url = f"{base}/ZHGXTV/Public/json/live_interface.txt"
        channels = []

        r = safe_get(url, self.HEADERS, self.TIMEOUT)
        if not r or r.status_code != 200:
            return channels

        try:
            data = r.json()
        except json.JSONDecodeError:
            return self._regex_fallback(r.text, base, "zhgxtv")

        items = (
            data.get("data")
            or data.get("channel_list")
            or data.get("list")
            or data.get("channels")
            or (data if isinstance(data, list) else [])
        )

        for ch in items:
            name = (
                ch.get("channel_name", "")
                or ch.get("name", "")
                or ch.get("title", "")
            )
            ch_id = str(
                ch.get("channel_id", "")
                or ch.get("id", "")
            )
            if name and ch_id:
                channels.append({
                    "name": name,
                    "url": f"{base}/hls/{ch_id}/index.m3u8",
                })

        return channels

    # --- 智能KUTV ---
    def _fetch_kutv(self, ip_port):
        """
        频道列表: /iptv/live/zh_cn.js (JS 变量)
        流地址:   http://ip:port/tsfile/live/{id}.m3u8
        id 格式:  0001_1, 0002_1 ...
        """
        base = f"http://{ip_port}"
        url = f"{base}/iptv/live/zh_cn.js"
        channels = []

        r = safe_get(url, self.HEADERS, self.TIMEOUT)
        if not r or r.status_code != 200:
            return channels

        text = r.text

        # 提取 JS 数组
        match = re.search(r"var\s+\w+\s*=\s*($$[\s\S]*?$$)\s*;", text)
        if match:
            try:
                items = json.loads(match.group(1))
                for ch in items:
                    name = (
                        ch.get("name", "")
                        or ch.get("title", "")
                        or ch.get("channel_name", "")
                    )
                    ch_id = str(
                        ch.get("id", "")
                        or ch.get("channel_id", "")
                    )
                    if name and ch_id:
                        channels.append({
                            "name": name,
                            "url": f"{base}/tsfile/live/{ch_id}.m3u8",
                        })
            except json.JSONDecodeError:
                pass

        if not channels:
            channels = self._regex_fallback(text, base, "kutv")

        return channels

    # --- 华视美达 ---
    def _fetch_huashi(self, ip_port):
        """
        流地址: http://ip:port/newlive/live/hls/{id}/live.m3u8
        无固定列表接口 → 先尝试常见 JSON 路径，失败则逐 ID 探测
        """
        base = f"http://{ip_port}"
        channels = []

        # 尝试常见列表接口
        list_urls = [
            f"{base}/newlive/channellist.json",
            f"{base}/newlive/live/channel_list.json",
            f"{base}/api/channel/list",
        ]

        for list_url in list_urls:
            r = safe_get(list_url, self.HEADERS, timeout=5)
            if not r or r.status_code != 200:
                continue

            text = r.text

            # m3u8 格式
            if "#EXTINF" in text:
                channels = self._parse_m3u8(text, base)
                if channels:
                    return channels

            # JSON 格式
            try:
                data = json.loads(text)
                items = (
                    data.get("data")
                    or data.get("channels")
                    or data.get("list")
                    or (data if isinstance(data, list) else [])
                )
                for ch in items:
                    name = (
                        ch.get("name", "")
                        or ch.get("title", "")
                        or ch.get("channel_name", "")
                    )
                    ch_id = str(
                        ch.get("id", "")
                        or ch.get("channel_id", "")
                    )
                    if name and ch_id:
                        channels.append({
                            "name": name,
                            "url": (
                                f"{base}/newlive/live/hls/"
                                f"{ch_id}/live.m3u8"
                            ),
                        })
                if channels:
                    return channels
            except json.JSONDecodeError:
                pass

        # 逐 ID 探测
        if not channels:
            channels = self._probe_huashi_ids(base)

        return channels

    def _probe_huashi_ids(self, base, max_id=200):
        """逐 ID 探测华视美达频道"""
        channels = []
        log("华视美达", f"逐 ID 探测 (1~{max_id})...", "step")

        def _check(cid):
            url = f"{base}/newlive/live/hls/{cid}/live.m3u8"
            try:
                r = requests.get(
                    url, headers=self.HEADERS,
                    timeout=3, verify=False,
                )
                if r.status_code == 200 and (
                    "#EXTINF" in r.text or len(r.text) > 50
                ):
                    m = re.search(r"#EXTINF.*?,(.+)", r.text)
                    name = m.group(1).strip() if m else f"频道{cid}"
                    return {"name": name, "url": url}
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(_check, i): i
                for i in range(1, max_id + 1)
            }
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    channels.append(result)

        channels.sort(key=lambda x: x["url"])
        return channels

    # --- 通用 ---

    def _parse_m3u8(self, text, base):
        """解析 m3u8 文本"""
        channels = []
        lines = text.strip().split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                m = re.search(r",(.+)$", line)
                name = m.group(1).strip() if m else f"频道{i}"
                if i + 1 < len(lines):
                    u = lines[i + 1].strip()
                    if not u.startswith("#"):
                        if not u.startswith("http"):
                            u = f"{base}{u}"
                        channels.append({"name": name, "url": u})
                        i += 2
                        continue
            i += 1
        return channels

    def _regex_fallback(self, text, base, tag):
        """正则兜底提取"""
        channels = []
        pairs = re.findall(
            r'"(?:channel_)?name"\s*:\s*"([^"]+)".*?'
            r'"(?:channel_)?id"\s*:\s*"([^"]+)"',
            text, re.DOTALL,
        )
        for name, ch_id in pairs:
            if tag == "zhgxtv":
                url = f"{base}/hls/{ch_id}/index.m3u8"
            elif tag == "kutv":
                url = f"{base}/tsfile/live/{ch_id}.m3u8"
            elif tag == "huashi":
                url = f"{base}/newlive/live/hls/{ch_id}/live.m3u8"
            else:
                url = ""
            channels.append({"name": name, "url": url})
        return channels


# ============================================================
# 第三阶段: 分组输出
# ============================================================
class ChannelExporter:
    """按省份地区+运营商分组输出 txt 文件"""

    def __init__(self, output_dir="iptv_channels"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def export(self, results):
        """将扫描结果分组并写入文件"""
        groups = defaultdict(list)

        for r in results:
            if not r["channels"]:
                continue
            fname = make_filename(
                r["province"], r["city"], r["operator"]
            )
            groups[fname].extend(r["channels"])

        # 去重
        for fname in groups:
            seen = set()
            unique = []
            for ch in groups[fname]:
                key = (ch["name"], ch["url"])
                if key not in seen:
                    seen.add(key)
                    unique.append(ch)
            groups[fname] = unique

        # 写入
        total_files = 0
        total_channels = 0
    
