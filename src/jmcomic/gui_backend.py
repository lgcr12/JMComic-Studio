"""Backend for the local JMComic Studio UI.

Owns the download queue, structured progress, album metadata and cover proxy so
`gui.py` only has to serve the page and route HTTP calls.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import zipfile
from html import unescape as html_unescape
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .jm_downloader import JmDownloader
from .jm_toolkit import JmcomicText


_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.jpe', '.png', '.webp', '.gif', '.avif'}


def parse_ids(text: str) -> tuple[list[str], list[str]]:
    albums: list[str] = []
    photos: list[str] = []
    for token in (item for item in re.split(r'[\s,，;；]+', text.strip()) if item):
        is_photo = token.lower().startswith('p')
        value = token[1:] if is_photo else token
        parsed = JmcomicText.parse_to_jm_id(value)
        (photos if is_photo else albums).append(parsed)
    return albums, photos


def natural_sort_key(value: str | Path) -> tuple[Any, ...]:
    parts = re.split(r'(\d+)', str(value).casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def safe_name(value: str, fallback: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or '').strip())
    return name.rstrip(' .')[:150] or fallback


def download_result_list(result: Any) -> list[Any]:
    if result is None:
        return []
    succeeded = getattr(result, 'succeeded', None)
    if succeeded is not None:
        return list(succeeded)
    return [result]


def pages_iter(page: Any):
    """Iterate (album_id, title) across jmcomic search page variants."""
    return page.iter_id_title()


_NORMALIZE_RE = re.compile(r'[\s\-_·・:：!！?？,，.。()（）\[\]【】「」『』/\\|]+')


_TRAD_TO_SIMP = str.maketrans(
    '教學祕密漫畫連載話說閱讀點擊喜歡觀看繁體這為與們個時間開關閉話號'
    '導覽選擇確認識別錯誤載入整理資料夾輸出目錄章節圖片檔案無雙愛戀龍鳳'
    '爭奪戰國語動態繪畫升級進度結束開始總頁數作者名的',
    '教学秘密漫画连载话说阅读点击喜欢观看繁体这为与们个时间开关闭话号'
    '导览选择确认识别错误载入整理资料夹输出目录章节图片档案无双爱恋龙凤'
    '争夺战国语动态绘画升级进度结束开始总页数作者名的',
)


def normalize_title(title: str) -> str:
    text = html_unescape(str(title or '')).lower().translate(_TRAD_TO_SIMP)
    text = _NORMALIZE_RE.sub('', text)
    return text


def _similar_title(left: str, right: str) -> bool:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 6 and shorter in longer


def export_venera(results: list[Any], output_dir: str, keep_raw: bool = False) -> tuple[int, int]:
    """Create one Venera-compatible ZIP archive per downloaded comic."""
    if not results:
        return 0, 0
    export_root = Path(output_dir).resolve().parent / 'Venera-导入'
    export_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    comics: dict[str, dict[str, Any]] = {}

    for result in results:
        detail = getattr(result, 'detail', None)
        album = getattr(result, 'album', None)
        if detail is None or album is None:
            continue
        manifest = getattr(result, 'manifest', None)
        image_paths = list(getattr(manifest, 'image_filepath_list', []) or [])
        if not image_paths:
            continue

        comic_name = safe_name(getattr(album, 'name', ''), f'JM{album.id}')
        comic_key = str(getattr(album, 'id', comic_name))
        comic = comics.setdefault(
            comic_key,
            {
                'name': comic_name,
                'staging': export_root / f'.{comic_name}-{comic_key}.staging',
                'chapters': {},
            },
        )
        staging_dir: Path = comic['staging']
        staging_dir.mkdir(parents=True, exist_ok=True)

        chapter_dirs: dict[str, Path] = {}
        for image_path in image_paths:
            source = Path(image_path)
            if source.suffix.lower() not in _IMAGE_SUFFIXES or not source.is_file():
                continue
            source_parent = str(source.parent.resolve())
            chapter_dir = chapter_dirs.get(source_parent) or comic['chapters'].get(source_parent)
            if chapter_dir is None:
                chapter_dir = staging_dir / safe_name(source.parent.name, '未命名章节')
                chapter_dirs[source_parent] = chapter_dir
                comic['chapters'][source_parent] = chapter_dir
                chapter_dir.mkdir(parents=True, exist_ok=True)
            destination = chapter_dir / source.name
            if source.resolve() == destination.resolve():
                continue
            shutil.copy2(source, destination)
            copied += 1

    for comic in comics.values():
        staging_dir: Path = comic['staging']
        if not staging_dir.is_dir():
            continue
        archive_path = export_root / f"[JM{safe_name(str(comic['name']), 'comic')}].zip"
        temp_archive = export_root / f'.{archive_path.name}.tmp'
        if temp_archive.exists():
            temp_archive.unlink()
        with zipfile.ZipFile(temp_archive, 'w', compression=zipfile.ZIP_STORED) as archive:
            for file_path in sorted(
                    staging_dir.rglob('*'),
                    key=lambda path: natural_sort_key(path.relative_to(staging_dir))):
                if file_path.is_file() and file_path.suffix.lower() in _IMAGE_SUFFIXES:
                    archive.write(file_path, Path(comic['name']) / file_path.relative_to(staging_dir))
        os.replace(temp_archive, archive_path)
        shutil.rmtree(staging_dir)

    if keep_raw:
        return len(comics), copied

    cleanup_paths: set[Path] = set()
    for result in results:
        manifest = getattr(result, 'manifest', None)
        if manifest is None:
            continue
        cleanup_paths.update(Path(path) for path in getattr(manifest, 'image_filepath_list', []) or [])
        for paths in getattr(manifest, 'export_filepath_dict', {}).values():
            cleanup_paths.update(Path(path) for path in paths or [])
    cleanup_dirs: set[Path] = set()
    for path in cleanup_paths:
        try:
            if path.is_file():
                cleanup_dirs.add(path.parent)
                path.unlink()
        except OSError:
            continue
    for directory in sorted(cleanup_dirs, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

    return len(comics), copied


def reencode_images(paths: list[Path], quality: int) -> int:
    """Re-encode JPEG files with the requested quality (only when < 100)."""
    if quality >= 100:
        return 0
    from PIL import Image

    done = 0
    for path in paths:
        try:
            if path.suffix.lower() not in {'.jpg', '.jpeg'}:
                continue
            with Image.open(path) as image:
                image.convert('RGB').save(path, 'JPEG', quality=quality, optimize=True)
            done += 1
        except Exception:  # noqa: BLE001
            continue
    return done


def export_pdfs(results: list[Any], orientation: str = 'auto') -> int:
    """Build one PDF per chapter folder from the downloaded images."""
    import img2pdf

    groups: dict[Path, list[Path]] = {}
    for result in results:
        manifest = getattr(result, 'manifest', None)
        for item in getattr(manifest, 'image_filepath_list', []) or []:
            path = Path(item)
            if path.suffix.lower() in _IMAGE_SUFFIXES and path.is_file():
                groups.setdefault(path.parent, []).append(path)
    if not groups:
        return 0
    layout = None
    if orientation == 'portrait':
        layout = img2pdf.get_layout_fun((595.0, 842.0))
    elif orientation == 'landscape':
        layout = img2pdf.get_layout_fun((842.0, 595.0))
    kwargs = {'layout_fun': layout} if layout is not None else {}
    count = 0
    for folder, paths in groups.items():
        pdf_path = folder.with_name(folder.name + '.pdf')
        try:
            ordered = sorted(paths, key=lambda item: natural_sort_key(item.name))
            with open(pdf_path, 'wb') as handle:
                handle.write(img2pdf.convert([str(item) for item in ordered], **kwargs))
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


class ProgressTracker:
    """Thread-safe progress shared with the downloader hooks."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.chapters_total = 0
        self.chapters_done = 0
        self.images_total = 0
        self.images_done = 0
        self.current_chapter = ''
        self.current_index = 0
        self.current_image = 0
        self.bytes_done = 0
        self.phase = '准备中'
        self.started_at = time.time()
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.cancelled = False
        self.speed = 0.0
        self._rate_at = time.time()
        self._rate_bytes = 0

    def pause(self) -> None:
        self.resume_event.clear()

    def resume(self) -> None:
        self.resume_event.set()

    def wait_if_paused(self) -> None:
        while not self.resume_event.wait(0.2):
            if self.cancelled:
                return

    def add_bytes(self, size: int) -> None:
        with self.lock:
            self.bytes_done += max(0, int(size))
            now = time.time()
            if now - self._rate_at >= 0.8:
                self.speed = (self.bytes_done - self._rate_bytes) / max(0.001, now - self._rate_at)
                self._rate_at = now
                self._rate_bytes = self.bytes_done

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            total = self.images_total or self.images_done
            ratio = (self.images_done / total) if total else 0.0
            remaining = max(0, total - self.images_done)
            avg = (self.bytes_done / self.images_done) if self.images_done else 0
            eta = int(remaining * avg / self.speed) if self.speed > 1024 and avg else -1
            return {
                'chapters_done': self.chapters_done,
                'chapters_total': self.chapters_total,
                'images_done': self.images_done,
                'images_total': self.images_total,
                'current_chapter': self.current_chapter,
                'current_index': self.current_index,
                'current_image': self.current_image,
                'bytes': self.bytes_done,
                'speed': round(self.speed, 1),
                'eta': eta,
                'ratio': round(min(1.0, max(0.0, ratio)), 4),
                'phase': self.phase,
            }


class GuiDownloader(JmDownloader):
    """JmDownloader that reports structured progress to a tracker."""

    tracker: Optional[ProgressTracker] = None

    def before_album(self, album):  # type: ignore[override]
        super().before_album(album)
        tracker = GuiDownloader.tracker
        if tracker is not None:
            with tracker.lock:
                tracker.chapters_total = len(album)
                tracker.images_total = album.page_count
                tracker.phase = f'下载本子 JM{album.id}'

    def before_photo(self, photo):  # type: ignore[override]
        super().before_photo(photo)
        tracker = GuiDownloader.tracker
        if tracker is not None:
            with tracker.lock:
                if not tracker.chapters_total:
                    tracker.chapters_total = 1
                if not tracker.images_total:
                    tracker.images_total = len(photo)
                tracker.current_chapter = getattr(photo, 'name', '')
                tracker.current_index = getattr(photo, 'album_index', 0)
                tracker.current_image = 0

    def after_image(self, image, img_save_path):  # type: ignore[override]
        super().after_image(image, img_save_path)
        tracker = GuiDownloader.tracker
        if tracker is not None:
            with tracker.lock:
                tracker.images_done += 1
                tracker.current_image += 1
            try:
                tracker.add_bytes(os.path.getsize(img_save_path))
            except OSError:
                pass
            tracker.wait_if_paused()

    def after_photo(self, photo):  # type: ignore[override]
        super().after_photo(photo)
        tracker = GuiDownloader.tracker
        if tracker is not None:
            with tracker.lock:
                tracker.chapters_done += 1
                tracker.current_chapter = ''
                tracker.current_image = 0


GuiDownloader.use()


class TaskLogHandler(logging.Handler):
    def __init__(self, task: 'Task') -> None:
        super().__init__()
        self.task = task

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.task.add_log(self.format(record))
        except Exception:
            self.handleError(record)


@dataclass
class Task:
    id: str
    albums: list[str]
    photos: list[str]
    option: str
    output: str
    format: str
    dry_run: bool
    quality: int = 100
    pdf_orientation: str = 'auto'
    skip_bad: bool = True
    keep_raw: bool = False
    supplement: list[dict[str, Any]] = field(default_factory=list)
    supplement_title: str = ''
    status: str = 'queued'
    logs: list[str] = field(default_factory=list)
    error: str = ''
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    tracker: Optional[ProgressTracker] = None
    stats: dict[str, Any] = field(default_factory=dict)
    control: Any = None
    _log_lock: threading.Lock = field(default_factory=threading.Lock)

    def add_log(self, line: str) -> None:
        with self._log_lock:
            self.logs.append(line)
            if len(self.logs) > 4000:
                del self.logs[:2000]

    def label(self) -> str:
        parts = []
        if self.albums:
            parts.append('本子 ' + '、'.join(self.albums[:3]))
        if self.photos:
            parts.append('章节 ' + '、'.join(self.photos[:3]))
        return ' / '.join(parts) or '空任务'


_ISSUE_PATTERNS = (
    re.compile(r'第\s*(\d+)\s*[话話章回集]'),
    re.compile(r'^\s*(\d+)\s*$'),
    re.compile(r'[\[\(（【]\s*(\d{1,4})\s*[\]\)）】]'),
    re.compile(r'^\s*(\d{1,4})'),
)
_ISSUE_RANGE = re.compile(r'(\d{1,4})\s*[-–~～至到]\s*(\d{1,4})')


def parse_issues(name: str) -> list[int]:
    """Extract issue numbers; ranges like "4-6" expand to [4, 5, 6]."""
    text = html_unescape(str(name or '')).strip()
    if not text:
        return []
    ranged = _ISSUE_RANGE.search(text)
    if ranged:
        start, end = int(ranged.group(1)), int(ranged.group(2))
        if 0 < start <= end and end - start <= 500:
            return list(range(start, end + 1))
    single = parse_issue(text)
    return [single] if single else []


def parse_issue(name: str) -> Optional[int]:
    """Extract the issue number from chapter names across sources."""
    text = html_unescape(str(name or '')).strip()
    if not text:
        return None
    for pattern in _ISSUE_PATTERNS:
        found = pattern.search(text)
        if found:
            try:
                value = int(found.group(1))
            except ValueError:
                continue
            if 0 < value < 100000:
                return value
    return None


def download_supplement(task: Task, tracker: ProgressTracker, control: Any) -> tuple[int, int]:
    """Download missing chapters from a secondary plain-HTTP source."""
    if not task.supplement:
        return 0, 0
    from .site_sources import fetch_image

    root = Path(task.output or '.').resolve() / (task.supplement_title or '补充章节')
    root.mkdir(parents=True, exist_ok=True)
    chapters = 0
    images = 0
    for entry in task.supplement:
        if control is not None and control.is_cancelled:
            break
        chapter_dir = root / safe_name(entry.get('name') or f"第{entry.get('index')}话", 'chapter')
        chapter_dir.mkdir(parents=True, exist_ok=True)
        tracker.phase = f"补充章节：{chapter_dir.name}"
        try:
            urls = _supplement_urls(entry)
        except Exception as error:  # noqa: BLE001
            task.add_log(f'{time.strftime("%H:%M:%S")}  WARNING  补充章节失败：{entry.get("name")}，{error}')
            continue
        if not urls:
            continue
        tracker.current_chapter = chapter_dir.name
        tracker.current_index = int(entry.get('index') or 0)
        with tracker.lock:
            tracker.images_total += len(urls)
        ok = 0
        for index, url in enumerate(urls, start=1):
            if control is not None and control.is_cancelled:
                break
            target = chapter_dir / f'{index:05d}.jpg'
            if target.exists() and target.stat().st_size > 0:
                ok += 1
                with tracker.lock:
                    tracker.images_done += 1
                continue
            try:
                payload = fetch_image(url)
                target.write_bytes(payload)
                ok += 1
                with tracker.lock:
                    tracker.images_done += 1
                    tracker.current_image += 1
                tracker.add_bytes(len(payload))
                time.sleep(0.15)
            except Exception as error:  # noqa: BLE001
                task.add_log(f'{time.strftime("%H:%M:%S")}  WARNING  图片失败：{url}，{error}')
            tracker.wait_if_paused()
        if ok:
            chapters += 1
            images += ok
            with tracker.lock:
                tracker.chapters_done += 1
    return chapters, images


def _supplement_urls(entry: dict[str, Any]) -> list[str]:
    from .site_sources import get_source

    source = get_source(entry.get('source') or 'rmtt')
    if source is None:
        raise ValueError(f"未知来源：{entry.get('source')}")
    return source.chapter_images(str(entry.get('comic') or entry.get('album') or ''), str(entry.get('id')))


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tasks: list[Task] = []
        self.current: Optional[Task] = None
        self.worker: Optional[threading.Thread] = None
        self.album_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.cover_cache: dict[str, bytes] = {}
        self.last_finished: Optional[Task] = None

    # ---------------------------------------------------------------- queue
    def submit(self, payload: dict[str, Any]) -> Task:
        albums_a, photos_a = parse_ids(str(payload.get('albums', '')))
        albums_b, photos_b = parse_ids(str(payload.get('photos', '')))
        albums = list(dict.fromkeys(albums_a + albums_b))
        photos = list(dict.fromkeys(photos_a + photos_b))
        supplement = list(payload.get('supplement') or [])
        if not albums and not photos and not supplement:
            raise ValueError('请至少填写一个本子或章节 ID')
        task = Task(
            id=f't{int(time.time() * 1000)}-{os.urandom(2).hex()}',
            albums=albums,
            photos=photos,
            option=str(payload.get('option', '')).strip(),
            output=str(payload.get('output', '')).strip(),
            format=str(payload.get('format', 'jpg')).strip().lower() or 'jpg',
            dry_run=bool(payload.get('dry_run')),
            quality=max(50, min(100, int(payload.get('quality') or 100))),
            pdf_orientation=str(payload.get('pdf_orientation') or 'auto'),
            skip_bad=bool(payload.get('skip_bad', True)),
            keep_raw=bool(payload.get('keep_raw', False)),
            supplement=list(payload.get('supplement') or []),
            supplement_title=str(payload.get('supplement_title') or ''),
        )
        with self.lock:
            self.tasks.append(task)
            self._ensure_worker()
        return task

    def _ensure_worker(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._loop, name='jmcomic-gui-queue', daemon=True)
            self.worker.start()

    def _next_task(self) -> Optional[Task]:
        with self.lock:
            for task in self.tasks:
                if task.status == 'queued':
                    return task
        return None

    def _loop(self) -> None:
        while True:
            task = self._next_task()
            if task is None:
                with self.lock:
                    self.worker = None
                return
            with self.lock:
                self.current = task
                task.status = 'running'
                task.started_at = time.time()
            try:
                self._run(task)
            finally:
                with self.lock:
                    task.finished_at = time.time()
                    self.last_finished = task
                    self.current = None
                    task.control = None

    def _run(self, task: Task) -> None:
        from .api import JmOption, create_option, download_album, download_photo
        from .jm_config import jm_logger
        from .jm_task_context import DownloadControl, jm_task_context

        control = DownloadControl()
        tracker = ProgressTracker()
        task.control = control
        task.tracker = tracker
        task.add_log(f'{time.strftime("%H:%M:%S")}  INFO  任务开始：{task.label()}')
        handler = TaskLogHandler(task)
        handler.setFormatter(logging.Formatter('%(asctime)s  %(levelname)s  %(message)s', datefmt='%H:%M:%S'))
        jm_logger.addHandler(handler)

        if task.dry_run:
            task.status = 'done'
            tracker.phase = '试运行完成'
            task.stats = {'dry_run': True, 'chapters': 0, 'images': 0, 'pdf_pages': 0, 'errors': 0, 'bytes': 0}
            task.add_log(f'{time.strftime("%H:%M:%S")}  INFO  试运行完成：未联网、未写文件')
            jm_logger.removeHandler(handler)
            return

        GuiDownloader.tracker = tracker
        results: list[Any] = []
        try:
            option = create_option(task.option) if task.option else JmOption.default()
            if task.output:
                option.dir_rule.base_dir = os.path.abspath(task.output)
            if task.format in {'jpg', 'pdf', 'both'}:
                option.download.image.suffix = '.jpg'
            with jm_task_context(task_id=task.id, control=control):
                if task.albums:
                    tracker.phase = '下载本子'
                    results.extend(download_result_list(
                        download_album(task.albums, option, control=control)
                    ))
                if task.photos and not control.is_cancelled:
                    tracker.phase = '下载章节'
                    results.extend(download_result_list(
                        download_photo(task.photos, option, control=control)
                    ))

            failures = sum(len(getattr(item, 'failed', {}) or {}) for item in results)
            if failures and not task.skip_bad:
                raise RuntimeError(f'{failures} 张图片下载失败（已关闭跳过异常图片）')

            if not control.is_cancelled and task.supplement:
                tracker.phase = '下载补充章节'
                extra_chapters, extra_images = download_supplement(task, tracker, control)
                task.add_log(
                    f'{time.strftime("%H:%M:%S")}  INFO  补充源完成：{extra_chapters} 章，{extra_images} 张图片'
                )

            image_paths: list[Path] = []
            for item in results:
                manifest = getattr(item, 'manifest', None)
                image_paths.extend(Path(path) for path in getattr(manifest, 'image_filepath_list', []) or [])
            if not control.is_cancelled and task.quality < 100:
                tracker.phase = '压缩图片'
                reencode_images(image_paths, task.quality)
            if not control.is_cancelled and task.format in {'pdf', 'both'}:
                tracker.phase = '生成 PDF'
                export_pdfs(results, task.pdf_orientation)
            if not control.is_cancelled:
                tracker.phase = '整理 Venera 目录'
                comics, images = export_venera(results, task.output or os.getcwd(), keep_raw=task.keep_raw)
                task.add_log(f'{time.strftime("%H:%M:%S")}  INFO  Venera 导入目录已更新：{comics} 本，{images} 张图片')
            if control.is_cancelled:
                task.status = 'canceled'
                task.error = '任务已取消'
            else:
                task.status = 'done'
                task.stats = {
                    'chapters': tracker.chapters_done,
                    'images': tracker.images_done,
                    'pdf_pages': tracker.images_done if task.format in {'pdf', 'both'} else 0,
                    'errors': failures,
                    'bytes': tracker.bytes_done,
                }
                tracker.phase = '已完成'
        except Exception as error:  # noqa: BLE001
            if control.is_cancelled:
                task.status = 'canceled'
                task.error = '任务已取消'
            else:
                task.status = 'failed'
                task.error = str(error)
                task.add_log(f'{time.strftime("%H:%M:%S")}  ERROR  {error}')
        finally:
            jm_logger.removeHandler(handler)
            GuiDownloader.tracker = None

    def pause(self, task_id: str = '') -> None:
        task = self._target(task_id)
        if task and task.tracker and task.status == 'running':
            task.tracker.pause()
            task.status = 'paused'
            task.add_log(f'{time.strftime("%H:%M:%S")}  WARNING  任务已暂停')

    def resume(self, task_id: str = '') -> None:
        task = self._target(task_id)
        if task and task.tracker and task.status == 'paused':
            task.tracker.resume()
            task.status = 'running'
            task.add_log(f'{time.strftime("%H:%M:%S")}  INFO  任务继续')

    def cancel(self, task_id: str = '') -> None:
        task = self._target(task_id)
        if task is None:
            return
        if task.status == 'queued':
            task.status = 'canceled'
            task.error = '任务已取消'
            return
        if task.status in {'running', 'paused'}:
            if task.control is not None:
                task.control.cancel('用户取消任务')
            if task.tracker is not None:
                task.tracker.cancelled = True
                task.tracker.resume()
            task.status = 'canceling'
            task.add_log(f'{time.strftime("%H:%M:%S")}  WARNING  正在取消任务…')

    def _target(self, task_id: str) -> Optional[Task]:
        with self.lock:
            if task_id:
                for task in self.tasks:
                    if task.id == task_id:
                        return task
                return None
            if self.current is not None:
                return self.current
            for task in reversed(self.tasks):
                if task.status in {'queued', 'running', 'paused'}:
                    return task
        return None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current = self.current
            last = self.last_finished
            tasks = list(self.tasks)
        active = current or last
        progress = active.tracker.snapshot() if active and active.tracker else {}
        if current is not None:
            running, paused = True, current.status == 'paused'
            status = self._status_text(current)
        else:
            running, paused = False, False
            if last is None:
                status = '准备就绪'
            elif last.status == 'done':
                status = '试运行完成 · 未联网、未写文件' if last.dry_run else '任务完成 · 文件已写入输出目录'
            elif last.status == 'failed':
                status = f'任务失败 · {last.error}' if last.error else '任务失败'
            elif last.status == 'canceled':
                status = '任务已取消'
            else:
                status = '准备就绪'
        queue_items = [
            {
                'id': task.id,
                'label': task.label(),
                'status': task.status,
                'ratio': (task.tracker.snapshot()['ratio'] if task.tracker else 0),
                'dry_run': task.dry_run,
            }
            for task in tasks[-8:]
        ]
        return {
            'running': running,
            'paused': paused,
            'status': status,
            'logs': list(active.logs[-300:]) if active else [],
            'progress': progress,
            'queue': queue_items,
            'current': {
                'id': current.id if current else '',
                'format': current.format if current else '',
                'output': current.output if current else '',
                'mode': 'dry' if current and current.dry_run else 'real',
            },
            'stats': (last.stats if last is not None else {}),
            'last_status': last.status if last is not None else '',
        }

    def _status_text(self, task: Task) -> str:
        tracker = task.tracker
        if tracker is None:
            return '准备中'
        snap = tracker.snapshot()
        if task.status == 'paused':
            return '已暂停'
        if task.status == 'canceling':
            return '正在取消…'
        if snap['images_total']:
            return f"下载中 · 第{snap['current_index']}章 {snap['images_done']}/{snap['images_total']}"
        return '下载中'

    # ---------------------------------------------------------------- data
    def _client(self, option_path: str = ''):
        from .api import JmOption, create_option
        option = create_option(option_path) if option_path else JmOption.default()
        return option.build_jm_client()

    def search(self, query: str, option_path: str = '', category: str = 'all', source: str = 'all') -> list[dict[str, Any]]:
        query = str(query or '').strip()
        if not query:
            raise ValueError('请输入搜索关键词')

        if source in {'all', 'rmtt'}:
            extra = self._search_extra(query)
        else:
            extra = []

        if source == 'rmtt':
            return extra[:12]

        client = self._client(option_path)
        if category == 'hanman':
            pages = [client.new_jm_client(impl='html').search_site(query, page=1, category='hanman')]
        elif category == 'japanese':
            html = client.new_jm_client(impl='html')
            pages = [html.search_site(query, page=1, category=kind, sub_category='japanese')
                     for kind in ('doujin', 'single', 'short')]
        else:
            pages = [client.search_site(query, page=1)]

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in pages:
            for album_id, title in pages_iter(page):
                album_id = str(album_id)
                if album_id in seen:
                    continue
                seen.add(album_id)
                items.append({
                    'id': album_id,
                    'title': title,
                    'cover': f'/api/cover?id={album_id}',
                    'author': '',
                    'pages': None,
                    'chapters': None,
                    'updated': '',
                    'source': 'jm',
                    'source_label': 'JM',
                })
        items = items[:12]
        self._enrich(items, client)
        merged = items + [item for item in extra if not any(
            _similar_title(item['title'], other.get('title', '')) for other in items)]
        return merged[:16]

    def _search_extra(self, query: str) -> list[dict[str, Any]]:
        from .site_sources import SOURCES

        results: list[dict[str, Any]] = []
        for key, source in SOURCES.items():
            try:
                for item in source.search(query):
                    item['source'] = key
                    item['source_label'] = getattr(source, 'label', key)
                    results.append(item)
            except Exception:
                continue
        return results

    def _enrich(self, items: list[dict[str, Any]], client) -> None:
        def work(item: dict[str, Any]) -> None:
            try:
                album = client.get_album_detail(item['id'])
                item['author'] = '、'.join(album.authors or []) or album.author
                item['pages'] = album.page_count
                item['chapters'] = len(album)
                item['updated'] = str(album.update_date or '')
                item['title'] = album.name or item['title']
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(work, item) for item in items[:8]]
            wait(futures, timeout=12)

    def album(self, album_id: str, source: str = 'jm') -> dict[str, Any]:
        album_id = str(album_id or '').strip()
        if not album_id.isdigit():
            raise ValueError('无效的本子 ID')
        if source and source != 'jm':
            from .site_sources import get_source

            impl = get_source(source)
            if impl is None:
                raise ValueError(f'未知来源：{source}')
            data = impl.album(album_id)
            data['source'] = source
            data['source_label'] = getattr(impl, 'label', source)
            data['episodes'] = [
                {'id': str(ch['key']), 'index': str(ch['index']), 'name': ch['name']}
                for ch in data.pop('chapters_list', [])
            ]
            data['cover'] = f'/api/cover?source={source}&id={album_id}'
            return data

        now = time.time()
        cached = self.album_cache.get(album_id)
        if cached and now - cached[0] < 900:
            return cached[1]
        album = self._client().get_album_detail(album_id)
        data = {
            'id': album_id,
            'title': album.name,
            'author': '、'.join(album.authors or []) or album.author,
            'pages': album.page_count,
            'chapters': len(album),
            'updated': str(album.update_date or ''),
            'cover': f'/api/cover?id={album_id}',
            'source': 'jm',
            'source_label': 'JM',
            'episodes': [
                {'id': str(pid), 'index': str(index), 'name': str(name)}
                for pid, index, name in album.episode_list
            ],
        }
        self.album_cache[album_id] = (now, data)
        return data

    def _match_extra(self, title: str, source_key: str) -> Optional[dict[str, Any]]:
        """Best-effort title match on the secondary source."""
        from .site_sources import get_source

        impl = get_source(source_key)
        if impl is None or not title:
            return None
        base_title = re.split(r'[/|｜]', str(title))[0].strip()
        candidates: list[dict[str, Any]] = []
        for query in dict.fromkeys([base_title, str(title).strip()]):
            if not query:
                continue
            try:
                candidates.extend(impl.search(query))
            except Exception:
                continue
        if not candidates:
            return None
        target = normalize_title(base_title)
        best = None
        best_score = 0
        for item in candidates:
            name = normalize_title(item.get('title', ''))
            if not name:
                continue
            if name == target:
                score = 3
            elif name in target or target in name:
                score = 2
            elif len(set(name) & set(target)) / max(1, len(set(target))) >= 0.6:
                score = 1
            else:
                continue
            if score > best_score:
                best, best_score = item, score
        return best

    def merge(self, jm_id: str, extra_source: str = 'rmtt', extra_id: str = '') -> dict[str, Any]:
        """Merge chapter lists by issue number, primary source first.

        JM 的章节名是纯数字（322、323…），rmtt 是「第N话-标题」，因此不能按标题
        对齐，只能抽出「话号」后取并集：主源保留自己的话，次源只补主源没有的话号。
        """
        jm_id = str(jm_id or '').strip()
        extra_id = str(extra_id or '').strip()
        primary = self.album(jm_id, 'jm') if jm_id.isdigit() else None
        matched = None
        if not extra_id and primary:
            matched = self._match_extra(primary.get('title', ''), extra_source)
            if matched:
                extra_id = matched['id']
        secondary = None
        if extra_id:
            try:
                secondary = self.album(extra_id, extra_source)
            except Exception:
                secondary = None

        episodes = list(primary.get('episodes', [])) if primary else []
        primary_issues: set[int] = set()
        for ep in episodes:
            primary_issues.update(parse_issues(ep.get('name', '')))
        primary_max = max(primary_issues) if primary_issues else 0

        supplement: list[dict[str, Any]] = []
        if secondary:
            seen_extra: set[int] = set()
            for ep in secondary.get('episodes', []):
                issues = parse_issues(ep.get('name', ''))
                if not issues:
                    continue
                issue = issues[0]
                if issue in primary_issues or issue in seen_extra:
                    continue
                seen_extra.add(issue)
                supplement.append({
                    'id': ep.get('id', ''),
                    'index': issue,
                    'name': ep.get('name', ''),
                    'source': secondary.get('source', extra_source),
                    'comic': str(extra_id),
                })
            supplement.sort(key=lambda item: item['index'])

        stats = {
            'primary_count': len(episodes),
            'primary_max_issue': primary_max,
            'secondary_count': len(secondary.get('episodes', [])) if secondary else 0,
            'supplement_count': len(supplement),
            'auto_matched': bool(matched),
        }
        return {
            'primary': primary,
            'secondary': secondary,
            'matched': matched,
            'episodes': episodes,
            'supplement': supplement,
            'stats': stats,
        }

    def cover(self, album_id: str, source: str = 'jm') -> bytes:
        album_id = str(album_id or '').strip()
        if not album_id.isdigit():
            raise ValueError('无效的本子 ID')
        cache_key = f'{source}:{album_id}'
        cached = self.cover_cache.get(cache_key)
        if cached:
            return cached
        if source and source != 'jm':
            from .site_sources import get_source

            impl = get_source(source)
            if impl is None:
                raise ValueError(f'未知来源：{source}')
            content = impl.cover(album_id)
        else:
            url = JmcomicText.get_album_cover_url(album_id)
            from curl_cffi import requests as curl_requests
            response = curl_requests.get(
                url,
                impersonate='chrome',
                timeout=20,
                headers={'Referer': 'https://18comic.vip/'},
            )
            if response.status_code != 200 or not response.content:
                raise RuntimeError(f'封面获取失败：HTTP {response.status_code}')
            content = bytes(response.content)
        if len(self.cover_cache) > 200:
            self.cover_cache.clear()
        self.cover_cache[cache_key] = content
        return content


