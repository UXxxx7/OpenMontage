# WhatsApp MVP - Remotion 预打包缓存
#
# `npx remotion still/render` 每次调用都会现场 bundle 整个工程（实测 20-40s）。
# 一个任务的 QA stills（5-6 张，视觉重试后 8-12 张）+ 整片渲染 = 同一份代码
# 被反复打包十来次，占掉任务总时长的一大块。预打包到 build/ 后所有 still/
# render 直接吃 bundle 目录，跳过打包；src/ 有改动（mtime 更新）时自动重建。
#
# 2026-07-10 事故（真实发生，非假设）：旧版本 `--out-dir build` 直接往正在
# 被别的调用读取的同一个目录里现场重写文件——一次开发环境下的手动 bundle
# 测试撞上了一个正在跑的真实任务，QA stills 六张全部渲染失败（不是画质问题，
# 是执行失败），apply_style 整体失败，优雅降级把没套模板的半成品当成品发给
# 了用户，且没有任何报错——降级机制把这个真实 bug 悄悄兜住了。
#
# 现在的做法：build/ 是一个符号链接，从不就地重写；每次重建都产出全新的
# 不可变目录（.bundle-cache/<generation>/），成功后原子性地把符号链接切过
# 去。正在读旧目录的调用完全不受影响（inode 还在，直到没人可能还在用了才
# 清理，保留最近两代）；新调用总是拿到一个完整一致的目录，不存在"读到一半"
# 的中间状态。重建本身还过一把跨进程文件锁（fcntl.flock）——威胁模型不止
# "我自己手动测试"，还包括多个 worker 进程、队友在另一台机器上开发。

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BUNDLE_TIMEOUT_S = 300
_CACHE_DIRNAME = ".bundle-cache"
_KEEP_GENERATIONS = 2  # 当前 + 上一代，保证切换瞬间仍在读上一代的调用不会被清理坑掉
_PROC_LOCK = threading.Lock()  # 同进程内的双重检查锁；跨进程保护见 _cross_process_lock


def _src_mtime(remotion_dir: Path) -> float:
    newest = 0.0
    for sub in ("src", "contracts"):
        base = remotion_dir / sub if sub == "src" else remotion_dir.parent / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file():
                m = p.stat().st_mtime
                if m > newest:
                    newest = m
    return newest


def _resolve_current(build_link: Path, src_time: float) -> Optional[Path]:
    """build 当前指向的目录，若存在且够新则返回其真实路径，否则 None。"""
    if not build_link.is_symlink() and not build_link.exists():
        return None
    try:
        target = build_link.resolve()
    except OSError:
        return None
    marker = target / "index.html"
    if target.is_dir() and marker.exists() and marker.stat().st_mtime >= src_time:
        return target
    return None


def _cross_process_lock(lock_path: Path):
    """跨进程互斥：多个 RQ worker 进程、或开发者手动执行的重建命令，都可能
    与正在运行的服务同时触碰 bundle。Windows 没有 fcntl——明确降级为只在
    本进程内互斥，写清楚而不是假装覆盖了（队友在 Windows 上部署多进程前
    需要补 msvcrt 版本的锁）。"""
    try:
        import fcntl
    except ImportError:
        logger.warning(
            "  remotion: 本机无 fcntl（Windows），bundle 重建只在本进程内互斥，"
            "跨进程仍可能竞争"
        )

        class _NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _NoopLock()

    class _FlockLock:
        def __enter__(self):
            self._f = open(lock_path, "w")
            fcntl.flock(self._f, fcntl.LOCK_EX)
            return self

        def __exit__(self, *a):
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            return False

    return _FlockLock()


def _cleanup_old_generations(cache_dir: Path, keep: set[str]) -> None:
    gens = sorted(
        (p for p in cache_dir.iterdir() if p.is_dir() and p.name not in keep),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in gens:
        shutil.rmtree(stale, ignore_errors=True)


def sync_public_asset(remotion_dir: Path, rel_path: str) -> None:
    """把项目 public/<rel_path> 同步补写进当前 bundle 目录。

    预打包的 bundle 在打包那一刻快照了整个 public/；而每单任务的素材
    （source.mp4 / qr.png）是打包之后才 staged 进项目 public/ 的——渲染和
    QA stills 只从 bundle 内部解析静态文件（2026-07-13 实测：--public-dir
    对预打包 serveUrl 无效），于是 bundle 一旦是热的，后续每一单的
    apply_style 全部 404 失败、被优雅降级吞掉，用户拿到没套模板的半成品。
    7-10"交回未剪视频"事故的直接根因即此（此前误诊为并发重写竞态——竞态
    是真实存在的另一个问题，但当时的 404 是这个）。

    往当前代目录补写按任务隔离的新文件是纯增量操作，不与"代目录不可变 +
    原子切换"的设计冲突；此刻没有可用 bundle 就什么都不做——之后的
    ensure_remotion_bundle 重打包会把项目 public/ 整体拷进去。"""
    remotion_dir = Path(remotion_dir).resolve()
    src_file = remotion_dir / "public" / rel_path
    if not src_file.exists():
        return
    build_link = remotion_dir / "build"
    try:
        target = build_link.resolve(strict=True)
    except OSError:
        return  # 还没有任何 bundle（首次任务），重打包时会带上
    if not (target / "index.html").exists():
        return
    dst = target / "public" / rel_path
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, dst)
    except OSError as e:
        # 补写失败不该搞垮管线——但渲染大概率会 404，把线索留在日志里
        logger.warning(f"  remotion: 素材补写进 bundle 失败（渲染可能 404）: {e}")


def ensure_remotion_bundle(remotion_dir: Path) -> Optional[str]:
    """返回可直接喂给 still/render 的 bundle 目录（真实路径）；不可用时返回
    None（调用方回退到按次打包）。"""
    remotion_dir = Path(remotion_dir).resolve()  # 相对路径+cwd 组合会把 out-dir 解析进嵌套目录（实测）
    cache_dir = remotion_dir / _CACHE_DIRNAME
    build_link = remotion_dir / "build"
    src_time = _src_mtime(remotion_dir)

    current = _resolve_current(build_link, src_time)
    if current:
        return str(current)

    with _PROC_LOCK, _cross_process_lock(remotion_dir / ".bundle.lock"):
        # 双重检查：等锁的这段时间，可能已经有别的调用（同进程或跨进程）重建好了
        current = _resolve_current(build_link, src_time)
        if current:
            return str(current)

        # 迁移：老代码把 build/ 当真实目录直接写，这台机器上可能还留着旧的。
        # 符号链接没法 replace 一个非空真实目录（EISDIR），先挪开。挪开后
        # build_link 这个路径本身就不存在了（见下面 previous 的记录逻辑，
        # 必须靠 legacy 变量记住它，不能再指望 build_link.is_symlink()）。
        legacy = None
        if build_link.exists() and not build_link.is_symlink():
            legacy = cache_dir / f"legacy-{uuid.uuid4().hex[:8]}"
            cache_dir.mkdir(exist_ok=True)
            build_link.rename(legacy)
            logger.info(f"  remotion: 迁移旧版 build/ 目录 -> {legacy.name}")

        npx = shutil.which("npx") or "npx"
        cache_dir.mkdir(exist_ok=True)
        new_dir = cache_dir / f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        logger.info("  remotion: 预打包 bundle（src 有更新或首次）...")
        try:
            r = subprocess.run(
                [npx, "remotion", "bundle", "--out-dir", str(new_dir)],
                cwd=remotion_dir, capture_output=True, text=True,
                timeout=_BUNDLE_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning(f"  remotion: bundle 失败（回退按次打包）: {e}")
            shutil.rmtree(new_dir, ignore_errors=True)
            return None
        if r.returncode != 0 or not (new_dir / "index.html").exists():
            logger.warning(f"  remotion: bundle 失败（回退按次打包）: {(r.stderr or '')[-300:]}")
            shutil.rmtree(new_dir, ignore_errors=True)
            return None

        # 切换前先记住上一代（在 replace 之前取，之后 build_link 就指向新的了），
        # 保留它一段时间，避免正在读旧目录的调用被过早清理坑了。刚迁移挪开的
        # legacy 目录同样适用——它此刻还可能正被"迁移前就已经拿到 build/ 这个
        # 字面路径"的调用读取，不能让它在这次 cleanup 里被立刻删掉（原 bug：
        # 迁移分支跑完后 build_link 已不是符号链接，下面这个判断永远是
        # False，legacy 从未进 keep，首次迁移当场就把它删了——正是这个 PR
        # 本该修的那种"重建时删掉正在读的目录"事故，只是换成在迁移路径复现）。
        previous = legacy
        if build_link.is_symlink():
            try:
                previous = build_link.resolve()
            except OSError:
                previous = None

        # 原子切换：先建临时符号链接再 rename——rename 单个符号链接是原子操作，
        # 不存在"旧链接已删、新链接未建"的窗口；任何时刻 build 要么是完整的
        # 旧目录，要么是完整的新目录，绝不会是"正在写一半"的状态。
        tmp_link = remotion_dir / f".build.tmp-{uuid.uuid4().hex[:8]}"
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(new_dir, target_is_directory=True)
        tmp_link.replace(build_link)

        keep = {new_dir.name}
        if previous and previous.parent == cache_dir:
            keep.add(previous.name)
        _cleanup_old_generations(cache_dir, keep)

        logger.info(f"  remotion: bundle 就绪 -> {new_dir.name}")
        return str(new_dir)
