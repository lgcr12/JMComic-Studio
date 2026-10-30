"""Additional comic sources besides JMComic.

Each source exposes the same tiny surface so the job manager can treat them
uniformly:

    source.search(query)            -> list[{id, title, cover, author, ...}]
    source.album(album_id)          -> {title, chapters:[{key, name, index}], ...}
    source.chapter_images(key)      -> list[str]  (image urls, in order)

Only plain HTTP is used: no JavaScript, no browser.
"""

from __future__ import annotations

import re
from typing import Any, Optional

BASE = 'https://rmtt5.com'
UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)


def _http(url: str, *, referer: str = BASE + '/', data: Optional[dict[str, str]] = None) -> str:
    from curl_cffi import requests as creq

    headers = {
        'User-Agent': UA,
        'Referer': referer,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    if data is None:
        resp = creq.get(url, headers=headers, impersonate='chrome', timeout=30)
    else:
        resp = creq.post(url, headers=headers, data=data, impersonate='chrome', timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f'HTTP {resp.status_code}: {url}')
    return resp.text


def fetch_image(url: str) -> bytes:
    """Fetch one image with retries and CDN host fallback.

    该站图片 CDN 在连续请求时会重置连接，因此失败后换域名重试，并随机退避。
    """
    import random
    import time

    from curl_cffi import requests as creq

    hosts = ['img.nnpic.xyz', 'p4.nnpic.xyz', 'p6.nnpic.xyz', 'p8.nnpic.xyz', 'p9.nnpic.xyz']
    candidates = [url]
    for host in hosts:
        swapped = re.sub(r'//[^/]+/', f'//{host}/', url, count=1)
        if swapped not in candidates:
            candidates.append(swapped)

    last_error: Exception | None = None
    for attempt in range(4):
        target = candidates[attempt % len(candidates)]
        try:
            resp = creq.get(
                target,
                headers={'User-Agent': UA, 'Referer': BASE + '/', 'Accept': 'image/avif,image/webp,image/*,*/*;q=0.8'},
                impersonate='chrome',
                timeout=45,
            )
            if resp.status_code == 200 and resp.content:
                return bytes(resp.content)
            last_error = RuntimeError(f'HTTP {resp.status_code}')
        except Exception as error:  # noqa: BLE001
            last_error = error
        time.sleep(0.6 + random.random() * 1.2)
    raise RuntimeError(f'图片获取失败：{last_error}')


_NORMALIZE_RE = re.compile(r'[\s\-_·・:：!！?？,，.。()（）\[\]【】「」『』/\\|]+')
_TRAD_TO_SIMP = str.maketrans(
    '教學祕密漫畫連載話說閱讀點擊喜歡觀看繁體這為與們個時間開'
    '導選擇確認錯誤載入整理資料夾輸出目錄章節圖片檔案無雙愛戀龍'
    '爭奪戰國語動態繪畫升級進度結束開始總頁數作者名的',
    '教学秘密漫画连载话说阅读点击喜欢观看繁体这为与们个时间开'
    '导选择确认错误载入整理资料夹输出目录章节图片档案无双爱恋龙'
    '争夺战国语动态绘画升级进度结束开始总页数作者名的',
)


def normalize(text: str) -> str:
    import html as html_module

    value = html_module.unescape(str(text or '')).lower().translate(_TRAD_TO_SIMP)
    return _NORMALIZE_RE.sub('', value)


class RmttSource:
    """肉漫天堂：搜索 / 详情 / 章节图片。"""

    key = 'rmtt'
    label = '肉漫天堂'

    def search(self, query: str) -> list[dict[str, Any]]:
        html = _http(BASE + '/cata.php', data={'key': query, 'submit': '搜索'})
        pattern = re.compile(r'href="(/comics-detail/(\d+))"[^>]*title="([^"]*)"')
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        # 前几条是站点固定的推荐位，真正匹配项排在后面，因此按标题相关性过滤。
        needle = normalize(query)
        for path, comic_id, title in pattern.findall(html):
            if comic_id in seen:
                continue
            seen.add(comic_id)
            name = clean_text(title)
            results.append({
                'id': comic_id,
                'title': name,
                'cover': f'/api/cover?source=rmtt&id={comic_id}',
                'author': '',
                'chapters': None,
                'pages': None,
                'source': 'rmtt',
                'source_label': self.label,
                'relevant': bool(needle) and needle in normalize(name),
            })
        relevant = [item for item in results if item['relevant']]
        chosen = relevant or results
        for item in chosen:
            item.pop('relevant', None)
        return chosen[:12]

    def album(self, comic_id: str) -> dict[str, Any]:
        if not str(comic_id).isdigit():
            raise ValueError('无效的本子 ID')
        html = _http(f'{BASE}/comics-detail/{comic_id}', referer=BASE + '/')
        title = _match(html, r'<h1[^>]*>(.*?)</h1>')
        title = re.sub(r'<[^>]+>', '', title).strip() or f'JM{comic_id}'
        chapters: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key, name in re.findall(r'href="/comics-read/' + str(comic_id) + r'/([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            if key in seen:
                continue
            seen.add(key)
            label = clean_text(name)
            chapters.append({'key': key, 'name': label, 'index': len(chapters) + 1})
        cover = _match(html, r'<img[^>]+(?:data-src|src)="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"')
        return {
            'id': str(comic_id),
            'title': title,
            'author': '',
            'pages': None,
            'chapters': len(chapters),
            'cover': cover or f'/api/cover?source=rmtt&id={comic_id}',
            'chapters_list': chapters,
            'source': self.label,
        }

    def chapter_images(self, comic_id: str, chapter_key: str) -> list[str]:
        url = f'{BASE}/comics-read/{comic_id}/{chapter_key}'
        html = _http(url, referer=f'{BASE}/comics-detail/{comic_id}')
        urls = re.findall(r'data-src="(https?://[^"]+\.(?:jpg|jpeg|png|webp|avif))"', html)
        if not urls:
            urls = re.findall(r'<img[^>]+src="(https?://(?!mc\.yandex)[^"]+\.(?:jpg|jpeg|png|webp|avif))"', html)
        ordered = []
        for item in urls:
            if item not in ordered:
                ordered.append(item)
        return ordered

    def cover(self, comic_id: str) -> bytes:
        album = self.album(comic_id)
        if not album.get('cover') or '/api/cover' in album['cover']:
            raise RuntimeError('未找到封面')
        return fetch_image(album['cover'])


def _match(html: str, pattern: str) -> str:
    found = re.search(pattern, html, re.S)
    return found.group(1) if found else ''


_PRIVATE_USE = re.compile(r'[\ue000-\uf8ff]')


def clean_text(raw: str) -> str:
    """Strip tags, decode entities and drop icon-font private-use glyphs."""
    import html as html_module

    text = re.sub(r'<[^>]+>', '', raw or '')
    text = html_module.unescape(text)
    text = _PRIVATE_USE.sub('', text)
    text = text.replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', text).strip()


SOURCES = {'rmtt': RmttSource()}


def get_source(key: str):
    return SOURCES.get(key or '')
