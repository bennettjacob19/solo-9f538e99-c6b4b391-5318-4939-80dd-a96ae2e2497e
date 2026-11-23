"""raw 虚拟磁盘镜像：4 KiB 块读写、块状态、整盘扫描打洞回收。

设计要点
--------
* 镜像是普通稀疏文件，逻辑容量 = block_count * 4KiB，宿主实占字节数
  以 ``stat.st_blocks * 512`` 为准，空洞不占空间。
* 扫描整段跳过已有空洞：用 ``SEEK_DATA`` / ``SEEK_HOLE`` 一次定位到下一段
  已分配数据，天然跳过空洞（包括之前回收时已打的洞）。
* 只对“已分配且内容全零”的 **整块** 调用
  ``fallocate(FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE)`` 打洞。
  打洞保持文件容量不变（KEEP_SIZE），打洞后读回仍是全零，因此
  块读值与盘容量都不变，只释放宿主空间。
* 每个 4KiB 块有一把锁：打洞前在锁内再次校验该块仍为全零，
  与并发写互斥。判零之后若该块被写入非零数据，重检会失败、跳过打洞，
  新数据绝不会被抹掉。
* 启动建盘（``create_image``）走 flock 串行 + 临时文件原子 rename：
  容量相符的已有盘原样复用，容量不符报错停启并保留原盘，
  建到一半被强杀不会留下"半盘"，重启清理残留后重建。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import glob
import os
import threading
from typing import Iterator, Optional

from .constants import (
    BLOCK_SIZE,
    DEFAULT_BLOCK_COUNT,
    FALLOC_FL_KEEP_SIZE,
    FALLOC_FL_PUNCH_HOLE,
    FSYNC_EVERY_PUNCHES,
    SEEK_DATA,
    SEEK_HOLE,
    STATE_DATA,
    STATE_HOLE,
    STATE_ZERO,
)

# ---------------------------------------------------------------------------
# libc fallocate(2) 封装（Python 3.11 没有 os.fallocate，直接调 libc）
# ---------------------------------------------------------------------------
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.fallocate.restype = ctypes.c_int
_libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]


def punch_hole(fd: int, offset: int, length: int) -> None:
    """对 [offset, offset+length) 打洞，保持文件大小不变。

    要求 offset/length 按文件系统块大小对齐，本项目固定按 4KiB 对齐。
    """
    rc = _libc.fallocate(
        fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, offset, length
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"fallocate(PUNCH_HOLE) failed: {os.strerror(err)}")


def _is_allocated(fd: int, offset: int) -> bool:
    """判断 offset 处是否落在已分配数据区（即不是空洞）。"""
    try:
        data_start = os.lseek(fd, offset, SEEK_DATA)
    except OSError as exc:
        # ENXIO: offset 之后再无数据，全是空洞
        if exc.errno == 6:  # errno.ENXIO
            return False
        raise
    hole_start = os.lseek(fd, offset, SEEK_HOLE)
    return data_start < hole_start


class DiskImage:
    """一个固定容量的 raw 稀疏镜像。"""

    def __init__(self, path: str, block_count: int):
        self.path = path
        self.block_count = block_count
        self.capacity = block_count * BLOCK_SIZE
        self._fd = os.open(path, os.O_RDWR)
        if os.fstat(self._fd).st_size != self.capacity:
            raise ValueError(
                f"镜像 {path} 大小 {os.fstat(self._fd).st_size} 与预期 {self.capacity} 不一致"
            )
        # 每块一把锁，写与打洞互斥；读用共享信号量
        self._block_locks = [threading.Lock() for _ in range(block_count)]

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    # -- 基本属性 ----------------------------------------------------------
    def actual_bytes(self) -> int:
        """宿主盘上实际占用的字节数（st_blocks 以 512B 计）。"""
        return os.fstat(self._fd).st_blocks * 512

    def stats(self) -> dict:
        return {
            "path": self.path,
            "block_size": BLOCK_SIZE,
            "block_count": self.block_count,
            "capacity_bytes": self.capacity,
            "actual_bytes": self.actual_bytes(),
            "hole_bytes": self.capacity - self.actual_bytes(),
        }

    def _check_index(self, index: int) -> None:
        if not isinstance(index, int) or not (0 <= index < self.block_count):
            raise IndexError(f"块号越界: {index}，合法范围 0..{self.block_count - 1}")

    # -- 虚拟机视角的 4KiB 块读写 ------------------------------------------
    def read_block(self, index: int) -> bytes:
        """读一个 4KiB 块；空洞读回也是全零（与普通磁盘一致）。"""
        self._check_index(index)
        with self._block_locks[index]:
            data = os.pread(self._fd, BLOCK_SIZE, index * BLOCK_SIZE)
        if len(data) < BLOCK_SIZE:
            data = data + b"\x00" * (BLOCK_SIZE - len(data))
        return data

    def write_block(self, index: int, data: bytes) -> None:
        """写一个 4KiB 块；写全零会分配宿主块（变成可回收的零块）。"""
        self._check_index(index)
        if not isinstance(data, (bytes, bytearray)) or len(data) != BLOCK_SIZE:
            raise ValueError(f"写入数据必须恰好为 {BLOCK_SIZE} 字节")
        with self._block_locks[index]:
            os.pwrite(self._fd, bytes(data), index * BLOCK_SIZE)

    def block_state(self, index: int) -> str:
        """返回 hole / zero / data。判定与打洞走同一把块锁。"""
        self._check_index(index)
        off = index * BLOCK_SIZE
        with self._block_locks[index]:
            if not _is_allocated(self._fd, off):
                return STATE_HOLE
            data = os.pread(self._fd, BLOCK_SIZE, off)
            return STATE_ZERO if data == b"\x00" * BLOCK_SIZE else STATE_DATA

    def snapshot(self) -> dict:
        """供网页展示的整盘快照（状态 + 容量/实占）。"""
        return {"stats": self.stats(), "blocks": [self.block_state(i) for i in range(self.block_count)]}

    # -- 回收扫描 -----------------------------------------------------------
    def reclaim(
        self,
        should_stop: Optional[callable] = None,
        on_event: Optional[callable] = None,
    ) -> Iterator[dict]:
        """生成器：扫描整盘，跳过空洞，给已写入且全零的整块打洞。

        每处理一个块 yield 一条事件 dict::

            {"index", "state": "punched"|"hole"|"data"|"stopped",
             "actual_bytes"}

        * ``should_stop()`` 返回 True 时在当前块边界优雅停止（已打的洞保留）。
        * 打洞前在块锁内重新校验：必须仍是已分配 + 全零，否则跳过，
          从而保证判零后到达的新写入不会被抹掉。
        * 只打 4KiB 对齐的整块；段首尾未对齐的部分保留不动。
        """
        should_stop = should_stop or (lambda: False)
        on_event = on_event or (lambda ev: None)
        punch_count = 0
        size = self.capacity

        # 用 SEEK_DATA 跳到下一段已分配数据，整段跳过已有空洞
        scan = 0
        while scan < size:
            try:
                data_start = os.lseek(self._fd, scan, SEEK_DATA)
            except OSError as exc:
                if exc.errno == 6:  # ENXIO：后面全是空洞
                    break
                raise
            if data_start >= size:
                break
            hole_start = min(os.lseek(self._fd, data_start, SEEK_HOLE), size)

            # 只处理段内按 4KiB 对齐的整块
            first_block = (data_start + BLOCK_SIZE - 1) // BLOCK_SIZE
            last_block = hole_start // BLOCK_SIZE  # 不含
            for index in range(first_block, last_block):
                if should_stop():
                    on_event({"index": index, "state": "stopped", "actual_bytes": self.actual_bytes()})
                    return
                off = index * BLOCK_SIZE
                lock = self._block_locks[index]
                with lock:
                    # 拿到锁后再确认一次：覆盖持锁等待期间收到的中断
                    if should_stop():
                        on_event({"index": index, "state": "stopped", "actual_bytes": self.actual_bytes()})
                        return
                    # 打洞前二次确认：已分配且全零（与 write_block 互斥）
                    if not _is_allocated(self._fd, off):
                        event = {"index": index, "state": STATE_HOLE, "actual_bytes": self.actual_bytes()}
                    elif os.pread(self._fd, BLOCK_SIZE, off) == b"\x00" * BLOCK_SIZE:
                        punch_hole(self._fd, off, BLOCK_SIZE)
                        punch_count += 1
                        if punch_count % FSYNC_EVERY_PUNCHES == 0:
                            os.fsync(self._fd)
                        event = {"index": index, "state": "punched", "actual_bytes": self.actual_bytes()}
                    else:
                        event = {"index": index, "state": STATE_DATA, "actual_bytes": self.actual_bytes()}
                on_event(event)
                yield event

            # 段内未对齐的零碎部分不打洞，直接进入下一段扫描
            scan = hole_start

        os.fsync(self._fd)


# ---------------------------------------------------------------------------
# 预置镜像
# ---------------------------------------------------------------------------
class ImageCapacityMismatchError(RuntimeError):
    """已有镜像容量与 BLOCK_COUNT 配置不一致。

    携带盘上容量与配置容量供调用方报告；抛出时原盘内容与空洞保持原样，
    启动流程应当就此停止。
    """

    def __init__(self, path: str, disk_bytes: int, configured_blocks: int):
        self.path = path
        self.disk_bytes = disk_bytes
        self.configured_blocks = configured_blocks
        self.configured_bytes = configured_blocks * BLOCK_SIZE
        disk_desc = (
            f"{disk_bytes // BLOCK_SIZE} 个 4KiB 块"
            if disk_bytes % BLOCK_SIZE == 0
            else "非整块容量"
        )
        super().__init__(
            f"镜像容量不符，已停止启动（原盘内容与空洞保留不动）: "
            f"{path} 盘上容量 {disk_bytes} B（{disk_desc}）"
            f" != 配置容量 {self.configured_bytes} B（BLOCK_COUNT={configured_blocks}）。"
            f"请修正 BLOCK_COUNT 或更换 IMAGE_PATH / 数据卷后重启。"
        )


def _clean_stale_build_files(path: str) -> None:
    """删除被强杀的建盘进程留下的临时文件。调用方必须已持有建盘锁。"""
    for stale in glob.glob(f"{path}.build.*"):
        try:
            os.remove(stale)
        except OSError:
            pass  # 残留清不掉不阻塞启动；后续建盘若真有问题会另行报错


def _write_preset_layout(fd: int, block_count: int) -> None:
    """向 fd 写入完整预置布局并 fsync（256 块时）::

        块 0..63    空洞（ftruncate 产生，不占宿主空间）
        块 64..127  写过的全零块（已分配、占空间、可回收）
        块 128..191 非零块（含块号标记，不可回收）
        块 192..255 空洞

    块数不足时按 1/4 比例划分，保证三类块都存在。
    """
    os.ftruncate(fd, block_count * BLOCK_SIZE)  # 先全是空洞
    quarter = max(1, block_count // 4)
    zero_start, zero_end = quarter, quarter * 2
    data_start, data_end = quarter * 2, quarter * 3

    # 写过的零块：显式写 4KiB 零，文件系统会分配宿主块
    for i in range(zero_start, zero_end):
        os.pwrite(fd, b"\x00" * BLOCK_SIZE, i * BLOCK_SIZE)
    # 非零块：写入带块号的可辨识图案
    for i in range(data_start, min(data_end, block_count)):
        marker = f"block-{i:04d}\n".encode()
        payload = (marker + b"\xaa" * BLOCK_SIZE)[:BLOCK_SIZE]
        os.pwrite(fd, payload, i * BLOCK_SIZE)
    os.fsync(fd)


def _fsync_dir(dir_path: str) -> None:
    """fsync 目录本身，保证 rename 结果落盘（宿主掉电也不丢镜像）。"""
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def create_image(path: str, block_count: int = DEFAULT_BLOCK_COUNT) -> DiskImage:
    """创建（或打开已有的）预置 raw 镜像。

    启动流程保证：

    * **容量相符即复用**：已有镜像容量与 ``block_count`` 一致时原样打开，
      原盘内容与已打的洞全部保留。
    * **容量不符即停止**：抛出 :class:`ImageCapacityMismatchError`，
      报告盘上容量与配置容量，不触碰原盘。
    * **原子建盘**：预置布局先写入临时文件 ``<path>.build.<pid>``，fsync 后
      ``rename`` 到正式路径——布局完整前正式镜像不存在，建到一半被强杀
      也不会把半盘当成好盘；重启后清掉残留临时文件重新建。
    * **多进程串行**：建盘前对 ``<path>.lock`` 取排他 flock，空卷上同时最多
      一个进程建盘，卷上预置数据最多占一份；其余进程阻塞等待，待布局
      完整后直接复用。
    """
    abspath = os.path.abspath(path)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)

    lock_fd = os.open(f"{abspath}.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # 阻塞直到拿到建盘锁
        _clean_stale_build_files(abspath)

        if os.path.exists(abspath):
            disk_bytes = os.path.getsize(abspath)
            if disk_bytes != block_count * BLOCK_SIZE:
                raise ImageCapacityMismatchError(abspath, disk_bytes, block_count)
        else:
            tmp = f"{abspath}.build.{os.getpid()}"
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o644)
                try:
                    _write_preset_layout(fd, block_count)
                finally:
                    os.close(fd)
                # 原子生效：只有布局完整的镜像才会出现在正式路径上
                os.rename(tmp, abspath)
                _fsync_dir(os.path.dirname(abspath))
            except BaseException:
                # 建盘失败（含键盘中断）：清掉本次临时文件，不留下半盘
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
    finally:
        os.close(lock_fd)  # 关闭即释放 flock

    return DiskImage(abspath, block_count)
