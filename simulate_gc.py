#!/usr/bin/env python3
"""精简版 GC 写放大模拟器（回放 SWLC swap_lifecycle_trace）。

模拟方案
--------
* block  : 数据失效时机 = 同一个 swap slot 被"复用"（即再次有效写入同一 slot）。
           SLOT_FREE 事件被忽略 —— 块设备收不到 discard，只有覆盖写才知道旧数据失效。
* zufs   : 数据失效时机 = trace 中的 SLOT_FREE（主机侧逻辑释放立即可见）。
* zufs-<router> : 在 zufs 失效语义之上叠加数据分流策略，主机写在两个
           open zone 之间选择；分流策略通过 Router 接口扩展（见文件末尾）。

执行框架
--------
* 最多同时 open 两个 zone（stream 0 / stream 1），只有 open zone 可写。
* 不分流：主机数据全部写 stream 0；GC 搬移数据写 --gc-stream（默认1）。
* 分流：主机写由 Router 在 stream 0 / stream 1 之间选择；GC 搬移数据
  固定写 --gc-stream（默认 1，即与"冷"数据共用；0 = 与热流共用）。
* GC 触发：free zone 数量 <= --gc-trigger-free(默认2) 时开始 GC；
           free zone 数量 >= --gc-stop-free(默认4) 后停止 GC；
           主机申请新 zone 时若 free zone <= --gc-stall-free(默认1)，主机停写，
           先 GC 腾出 free zone 再继续（本模拟无时间轴，GC 逐 victim 原子完成，
           因此停写条件只作为安全兜底并计数）。
* victim 选择：greedy，取有效页最少的已写满 zone。
* 物理容量 = (--data-zones + --op-zones) 个 zone，zone 大小 --zone-mib。

写放大 WA = (主机写入页数 + GC 搬移页数) / 主机写入页数。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------------------
# SWLC trace 解析（与 analyze_swap_lifecycle.py 保持一致的 ABI）
# ---------------------------------------------------------------------------

HEADER_FMT = "<8s4I6Q2I8Q4Q64s16s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
RECORD_FMT = "<QQHHIIIQQQQQQQIIIIIiIBBH72s"
RECORD = struct.Struct(RECORD_FMT)
RECORD_SIZE = RECORD.size
MAGIC = b"SWLC\x00\x00\x00\x00"
ENDIAN_MARKER = 0x01020304

# record 字段在 iter_unpack 元组里的下标（对应 RECORD_FMT 的顺序）
IDX_EVENT_TYPE = 2
IDX_SWAP_ENTRY = 7
IDX_VMA_START = 9        # vma->vm_start
IDX_VMA_END = 10         # vma->vm_end，(end-start)>>12 即 VMA 页数
IDX_SECTOR = 13
IDX_NR_PAGES = 14
IDX_RESULT = 16
IDX_PAGE_STATE = 18
IDX_OOM_SCORE_ADJ = 19
IDX_VMA_TYPE = 22
IDX_VMA_NAME = 24

EV_ALLOC = 1
EV_VMA_BIND = 2
EV_SWAP_OUT = 3
EV_SWAPIN_FAULT = 4
EV_SWAPIN_IO = 5
EV_SLOT_FREE = 6
EV_DISCARD = 7
EV_ALIVE = 8
INEFFECTIVE_RESULTS = {4, 5, 6}      # RETRY / ERROR / ROLLBACK
UNKNOWN_RAW_SCORE = -(1 << 31)       # oom_score_adj 哨兵值：未知/未绑定 VMA


def read_header(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE or header[:8] != MAGIC:
        raise ValueError("输入不是 SWLC raw trace")
    version, header_size, record_size, endian = struct.unpack_from("<4I", header, 8)
    if header_size != HEADER_SIZE or record_size != RECORD_SIZE or endian != ENDIAN_MARKER:
        raise ValueError(
            f"不支持的 SWLC ABI: version={version} header={header_size} "
            f"record={record_size}（本工具期望 {HEADER_SIZE}/{RECORD_SIZE}）")
    declared = struct.unpack_from("<Q", header, 40)[0]   # records（6Q 的第 3 个）
    available = (path.stat().st_size - HEADER_SIZE) // record_size
    return {
        "version": version,
        "record_size": record_size,
        "records": min(declared, available),
    }


def iter_records(path: Path, count: int, batch_size: int = 1 << 16,
                 progress: bool = True):
    header = read_header(path)
    record = RECORD
    record_size = header["record_size"]
    processed = 0
    step = max(1, count // 200)
    next_report = step
    started = time.monotonic()
    with path.open("rb") as stream:
        stream.seek(HEADER_SIZE)
        while processed < count:
            number = min(batch_size, count - processed)
            raw = stream.read(number * record_size)
            if len(raw) != number * record_size:
                raise ValueError("trace 在 batch 中间截断")
            yield from record.iter_unpack(raw)
            processed += number
            if progress and processed >= next_report:
                elapsed = max(0.001, time.monotonic() - started)
                print(f"\r回放: {processed:,}/{count:,} "
                      f"({100.0 * processed / count:5.1f}%) "
                      f"{processed / elapsed:,.0f} rec/s",
                      end="", file=sys.stderr, flush=True)
                next_report += step
    if progress:
        print(file=sys.stderr)


# ---------------------------------------------------------------------------
# 分流策略接口：新策略在此扩展并注册进 ROUTERS
# ---------------------------------------------------------------------------

STREAM_HOT = 0   # 不分流时的主机流
STREAM_COLD = 1  # GC 目标流；分流时也是"冷"数据流


class Router:
    """把一次主机写路由到 stream 0（热）或 stream 1（冷，与 GC 共用）。

    ctx = (score, page_state, vma_type, vma_name, vma_pages)
    score 来自 VMA_BIND / SWAP_OUT 记录，未知时为 None。
    vma_name / vma_pages 优先取 SWAP_OUT 记录自带的 vma 信息（更新鲜），
    缺失时回退到之前 VMA_BIND 记录的信息；vma_pages=0 表示未知。
    """

    name = "base"

    def route(self, ctx) -> int:
        raise NotImplementedError


class ScoreRouter(Router):
    """示例策略：score 高于阈值判为冷数据写 stream 1。仅作接口演示。"""

    def __init__(self, threshold: int = 800):
        self.name = f"score{threshold}"
        self.threshold = threshold

    def route(self, ctx) -> int:
        score = ctx[0]
        if score is not None and score >= self.threshold:
            return STREAM_COLD
        return STREAM_HOT

class NameRouter(Router):
    def __init__(self):
        self.name = "name"

    def route(self, ctx) -> int:
        name = ctx[3].lower()  # vma_name
        if name.startswith(b"dalvik-") and b"dex data" in name:
            return STREAM_COLD
        return STREAM_HOT


class NameFullRouter(Router):
    """完整版按名字分流（学弟版规则的完整实现）：

    * dalvik-…dex data                     -> 冷（长寿）
    * 名字含 "file" 且 VMA 大于 size_pages 页 -> 冷（长寿）
      （trace 中文件页/shmem 的 vma_name 是 "[file]"，其 VMA 普遍很大）
    其余 -> 热。vma_pages 未知(0)时不会误判为大 VMA。
    """

    def __init__(self, size_pages: int = 4096):
        self.name = f"namefull{size_pages}"
        self.size_pages = size_pages

    def route(self, ctx) -> int:
        name = ctx[3].lower()
        if name.startswith(b"dalvik-") and b"dex data" in name:
            return STREAM_COLD
        if b"file" in name and ctx[4] > self.size_pages:
            return STREAM_COLD
        return STREAM_HOT


class HistoryRouter(Router):
    """在线自适应分流：按 vma_name 统计历史寿命，长寿概率高的名字判冷。

    证据结算不需要预知未来：
    * 数据失效（SLOT_FREE / 覆盖写）时，若存活 < T 页记一次短寿证据，
      否则记长寿证据；
    * 存活满 T 页仍未失效的数据立即记长寿证据（懒惰老化），
      之后真正失效时不再重复计数。
    路由：该名字 (long+prior) > p/(1-p) * (short+prior) 时判冷。
    以上信息（写入名字、slot free）zufs 主机侧全部可见，内核可实现。
    """

    def __init__(self, T: int = 540672, p: float = 0.7, prior: int = 5):
        # T 默认 2 个 zone（1056MiB zone = 270336 页）：寿命超过 2 个 zone
        # 的写入判为长寿。p=0.7：长寿证据占比超过 70% 的名字才判冷。
        self.name = f"hist{p}"
        self.T = T
        self.ratio = p / (1.0 - p)
        self.prior = prior
        self.hp = 0                        # 已写主机页计数（逻辑时钟）
        self.birth: dict[int, list] = {}   # key -> [birth_hp, name, settled]
        self.fifo: deque = deque()         # (key, birth_hp) 按出生序
        self.stats: dict[bytes, list] = {} # name -> [short, long]
        self._last_name = b""

    def _evidence(self, gkey: bytes, long_: bool) -> None:
        st = self.stats.get(gkey)
        if st is None:
            st = self.stats[gkey] = [0, 0]
        st[long_] += 1

    def _settle_aged(self) -> None:
        cut = self.hp - self.T
        fifo, birth = self.fifo, self.birth
        while fifo and fifo[0][1] <= cut:
            key, bhp = fifo.popleft()
            rec = birth.get(key)
            if rec is not None and rec[0] == bhp and not rec[2]:
                rec[2] = True
                self._evidence(rec[1], True)

    def route(self, ctx) -> int:
        self._settle_aged()
        gkey = ctx[3]
        self._last_name = gkey             # observe_write 紧随其后使用
        st = self.stats.get(gkey)
        if st is not None and \
                (st[1] + self.prior) > self.ratio * (st[0] + self.prior):
            return STREAM_COLD
        return STREAM_HOT

    # Simulator 在每次主机写完成后调用（key 与刚 route 的 ctx 对应）
    def observe_write(self, key: int) -> None:
        self.birth[key] = [self.hp, self._last_name, False]
        self.fifo.append((key, self.hp))
        self.hp += 1

    # Simulator 在数据失效（free / 覆盖写）时调用
    def observe_invalidate(self, key: int) -> None:
        rec = self.birth.pop(key, None)
        if rec is not None and not rec[2]:
            self._evidence(rec[1], self.hp - rec[0] > self.T)


ROUTERS = {
    "score": lambda: ScoreRouter(),
    "name": lambda: NameRouter(),
    "namefull": lambda: NameFullRouter(),
    "hist": lambda: HistoryRouter(),
}


# ---------------------------------------------------------------------------
# 核心模拟器
# ---------------------------------------------------------------------------

class Zone:
    __slots__ = ("zid", "cap", "wp", "valid")

    def __init__(self, zid: int, cap: int):
        self.zid = zid
        self.cap = cap
        self.wp = 0
        self.valid: set[int] = set()

    def reset(self) -> None:
        self.wp = 0
        self.valid.clear()

    @property
    def full(self) -> bool:
        return self.wp >= self.cap


class Simulator:
    def __init__(self, name: str, *, data_zones: int, op_zones: int,
                 zone_pages: int, invalidate_on: str,
                 router: Router | None = None,
                 gc_trigger_free: int = 2, gc_stall_free: int = 1,
                 gc_stop_free: int = 4, gc_stream: int = STREAM_COLD,
                 gc_age_split: int | None = None):
        assert invalidate_on in ("reuse", "free")
        self.name = name
        self.zone_pages = zone_pages
        self.invalidate_on = invalidate_on
        self.router = router
        self.gc_trigger_free = gc_trigger_free
        self.gc_stall_free = gc_stall_free
        self.gc_stop_free = gc_stop_free
        self.gc_stream = gc_stream      # GC 搬移数据写哪个 stream（默认冷流）
        # 年龄分代 GC：搬移时数据年龄(主机写页数) >= 该值写 stream 1（老年代），
        # 否则写 stream 0（与新数据混合、给第二次机会）。None = 关闭。
        self.gc_age_split = gc_age_split
        self._births: dict[int, int] | None = \
            {} if gc_age_split is not None else None
        # 在线策略（如 HistoryRouter）需要观察写入与失效事件
        self._observe = router is not None and hasattr(router, "observe_write")

        total = data_zones + op_zones
        self.free_zones: list[Zone] = [Zone(i, zone_pages) for i in range(total)]
        self.closed_zones: list[Zone] = []      # 已写满、可作 GC victim
        self.open_zones: list[Zone | None] = [None, None]
        self.l2p: dict[int, Zone] = {}          # slot_key -> 所在 zone

        # 统计
        self.host_pages = 0
        self.gc_pages = 0
        self.gc_rounds = 0
        self.zones_erased = 0
        self.stall_events = 0
        self.invalid_by_overwrite = 0
        self.invalid_by_free = 0
        self.stream_pages = [0, 0]
        self.seen_slots: set[int] = set()
        self.in_gc = False

    # -- 失效 ---------------------------------------------------------------

    def _invalidate(self, key: int, by_free: bool) -> None:
        zone = self.l2p.pop(key, None)
        if zone is None:
            return
        zone.valid.discard(key)
        if by_free:
            self.invalid_by_free += 1
        else:
            self.invalid_by_overwrite += 1

    def on_free(self, key: int) -> None:
        if self.invalidate_on == "free":
            self._invalidate(key, by_free=True)
            if self._observe:
                self.router.observe_invalidate(key)

    # -- 写入 ---------------------------------------------------------------

    def host_write(self, key: int, ctx) -> None:
        # 覆盖写：两种模型下旧数据都在此刻失效（block 只有这一条失效路径）
        if key in self.l2p:
            self._invalidate(key, by_free=False)
            if self._observe:
                self.router.observe_invalidate(key)
        stream = self.router.route(ctx) if self.router else STREAM_HOT
        self._append(stream, key, is_gc=False)
        if self._births is not None:
            self._births[key] = self.host_pages
        self.host_pages += 1
        self.stream_pages[stream] += 1
        self.seen_slots.add(key)
        if self._observe:
            self.router.observe_write(key)

    def _append(self, stream: int, key: int, *, is_gc: bool) -> None:
        # 循环而不是一次性分配：主机路径申请新 zone 时可能触发 GC，而 GC
        # 也可能写同一个 stream（分流时主机与 GC 共用 stream 1），所以每轮
        # 都重新读取 open_zones 的最新状态，写满的 zone 立即摘下再关闭，
        # 避免被重复 close。
        while True:
            zone = self.open_zones[stream]
            if zone is not None and not zone.full:
                break
            if zone is not None:
                self.open_zones[stream] = None
                self._close(zone)
            self._open_new(stream, is_gc=is_gc)
        zone.wp += 1
        zone.valid.add(key)
        self.l2p[key] = zone

    def _close(self, zone: Zone) -> None:
        if zone.valid:
            self.closed_zones.append(zone)
        else:                       # 写满但已全部失效，直接擦除回收
            zone.reset()
            self.zones_erased += 1
            self.free_zones.append(zone)

    def _open_new(self, stream: int, *, is_gc: bool) -> None:
        if is_gc:
            if not self.free_zones:
                raise RuntimeError(
                    f"[{self.name}] GC 无 free zone 可用，OP 不足或逻辑容量超限")
            self.open_zones[stream] = self.free_zones.pop()
            return
        # 主机申请新 zone：free <= gc_stall_free 时主机停写，等 GC 回收到
        # 停止水线（gc_stop_free）后再继续
        if len(self.free_zones) <= self.gc_stall_free:
            self.stall_events += 1
            while len(self.free_zones) < self.gc_stop_free:
                if not self._gc_once():
                    break
            if len(self.free_zones) <= self.gc_stall_free:
                raise RuntimeError(
                    f"[{self.name}] 无可回收空间（victim 全部有效）："
                    "增加 --op-zones 或检查逻辑容量")
            # 停写期间 GC 可能已重新打开该流并留有空间，交回 _append 复查
            zone = self.open_zones[stream]
            if zone is not None and not zone.full:
                return
        self.open_zones[stream] = self.free_zones.pop()
        if len(self.free_zones) <= self.gc_trigger_free:
            while len(self.free_zones) < self.gc_stop_free:
                if not self._gc_once():
                    break

    # -- GC -----------------------------------------------------------------

    def _gc_once(self) -> bool:
        """回收一个 victim；无法取得进展时返回 False。"""
        if not self.closed_zones or self.in_gc:
            return False
        victim = min(self.closed_zones, key=lambda z: len(z.valid))
        if len(victim.valid) >= victim.cap:
            return False            # 搬满 zone 不产生 free 空间
        self.closed_zones.remove(victim)
        self.in_gc = True
        try:
            if self.gc_age_split is None:
                for key in victim.valid:
                    self._append(self.gc_stream, key, is_gc=True)
            else:
                # 年龄分代：老数据晋升 stream 1，年轻幸存者回 stream 0
                births = self._births
                cutoff = self.host_pages - self.gc_age_split
                for key in victim.valid:
                    stream = STREAM_COLD if births.get(key, 0) <= cutoff \
                        else STREAM_HOT
                    self._append(stream, key, is_gc=True)
            self.gc_pages += len(victim.valid)
        finally:
            self.in_gc = False
        victim.reset()
        self.zones_erased += 1
        self.free_zones.append(victim)
        self.gc_rounds += 1
        return True

    # -- 报告 ---------------------------------------------------------------

    def report(self) -> dict:
        total_writes = self.host_pages + self.gc_pages
        live = sum(len(z.valid) for z in self.closed_zones)
        live += sum(len(z.valid) for z in self.open_zones if z)
        return {
            "model": self.name,
            "host_pages": self.host_pages,
            "gc_pages": self.gc_pages,
            "waf": total_writes / self.host_pages if self.host_pages else 0.0,
            "gc_rounds": self.gc_rounds,
            "zones_erased": self.zones_erased,
            "stall_events": self.stall_events,
            "invalid_by_overwrite": self.invalid_by_overwrite,
            "invalid_by_free": self.invalid_by_free,
            "stream0_pages": self.stream_pages[0],
            "stream1_host_pages": self.stream_pages[1],
            "unique_slots": len(self.seen_slots),
            "live_pages_at_end": live,
        }


# ---------------------------------------------------------------------------
# trace 回放
# ---------------------------------------------------------------------------

def replay(args, models: list[Simulator]) -> dict:
    header = read_header(args.trace)
    count = header["records"]
    if args.max_records:
        count = min(count, args.max_records)
    pending: dict[int, tuple] = {}   # VMA_BIND 附带的 (score, vma_name, vma_pages)
    effective_swapouts = 0

    for values in iter_records(args.trace, count, progress=not args.no_progress):
        event = values[IDX_EVENT_TYPE]
        if event == EV_SWAP_OUT:
            if values[IDX_RESULT] in INEFFECTIVE_RESULTS:
                continue
            base = values[IDX_SWAP_ENTRY]
            nr = values[IDX_NR_PAGES] or 1
            score = values[IDX_OOM_SCORE_ADJ]
            page_state = values[IDX_PAGE_STATE]
            vma_type = values[IDX_VMA_TYPE]
            vma_name = values[IDX_VMA_NAME].split(b"\0", 1)[0]
            vstart = values[IDX_VMA_START]
            vend = values[IDX_VMA_END]
            vma_pages = (vend - vstart) >> 12 if vend > vstart else 0
            for i in range(nr):
                key = base + i
                p = pending.pop(key, None)
                eff_score, eff_name, eff_pages = score, vma_name, vma_pages
                if p is not None:
                    if p[0] != UNKNOWN_RAW_SCORE:
                        eff_score = p[0]
                    if not vma_pages and not vma_name:   # SWAP_OUT 无 vma 信息
                        eff_name, eff_pages = p[1], p[2]
                ctx = (None if eff_score == UNKNOWN_RAW_SCORE else eff_score,
                       page_state, vma_type, eff_name, eff_pages)
                for model in models:
                    model.host_write(key, ctx)
            effective_swapouts += 1
        elif event == EV_SLOT_FREE:
            base = values[IDX_SWAP_ENTRY]
            nr = values[IDX_NR_PAGES] or 1
            for i in range(nr):
                key = base + i
                pending.pop(key, None)
                for model in models:
                    model.on_free(key)
        elif event == EV_VMA_BIND:
            score = values[IDX_OOM_SCORE_ADJ]
            vstart = values[IDX_VMA_START]
            vend = values[IDX_VMA_END]
            vma_pages = (vend - vstart) >> 12 if vend > vstart else 0
            if score != UNKNOWN_RAW_SCORE or vma_pages:
                rec = (score, values[IDX_VMA_NAME].split(b"\0", 1)[0], vma_pages)
                base = values[IDX_SWAP_ENTRY]
                for i in range(values[IDX_NR_PAGES] or 1):
                    pending[base + i] = rec
        elif event == EV_ALLOC:
            base = values[IDX_SWAP_ENTRY]
            for i in range(values[IDX_NR_PAGES] or 1):
                pending.pop(base + i, None)

    return {"effective_swapout_events": effective_swapouts}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_models(args) -> list[Simulator]:
    geometry = dict(
        data_zones=args.data_zones,
        op_zones=args.op_zones,
        zone_pages=args.zone_pages,
        gc_trigger_free=args.gc_trigger_free,
        gc_stall_free=args.gc_stall_free,
        gc_stop_free=args.gc_stop_free,
        gc_stream=args.gc_stream,
        gc_age_split=(int(args.gc_age_split_zones * args.zone_pages)
                      if args.gc_age_split_zones else None),
    )
    models = []
    for name in args.models.split(","):
        name = name.strip()
        if not name:
            continue
        if name == "block":
            models.append(Simulator("block", invalidate_on="reuse", **geometry))
        elif name == "zufs":
            models.append(Simulator("zufs", invalidate_on="free", **geometry))
        elif name.startswith("zufs-"):
            router_name = name[len("zufs-"):]
            if router_name not in ROUTERS:
                raise SystemExit(f"未知分流策略: {router_name}，"
                                 f"可选: {', '.join(ROUTERS)}")
            models.append(Simulator(name, invalidate_on="free",
                                    router=ROUTERS[router_name](), **geometry))
        else:
            raise SystemExit(f"未知模型: {name}")
    return models


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="精简 GC 写放大模拟器：block / zufs / zufs+分流")
    parser.add_argument("trace", nargs="?", type=Path,
                        default=Path(__file__).resolve().parent /
                        "candycrushsaga_swap_lifecycle.raw")
    parser.add_argument("--models", default="block,zufs",
                        help="逗号分隔: block, zufs, zufs-<router>"
                             f"（router 可选: {', '.join(ROUTERS)}）")
    parser.add_argument("--data-zones", type=int, default=8,
                        help="初始（数据）zone 数量")
    parser.add_argument("--op-zones", type=int, default=3,
                        help="over-provisioning zone 数量")
    parser.add_argument("--zone-mib", type=int, default=1056,
                        help="zone 大小 (MiB)")
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--gc-trigger-free", type=int, default=2,
                        help="free zone <= 此值时触发 GC")
    parser.add_argument("--gc-stall-free", type=int, default=1,
                        help="free zone <= 此值时主机停写等 GC")
    parser.add_argument("--gc-stop-free", type=int, default=4,
                        help="free zone >= 此值时停止 GC")
    parser.add_argument("--gc-stream", type=int, default=1, choices=(0, 1),
                        help="GC 搬移数据写入的 stream："
                             "1=与冷流共用（默认），0=与热流共用")
    parser.add_argument("--gc-age-split-zones", type=float, default=0,
                        help="年龄分代 GC：搬移时数据年龄超过 N 个 zone 的"
                             "写主机页数则晋升 stream 1，否则回 stream 0；"
                             "0=关闭（全部写 --gc-stream）")
    parser.add_argument("--max-records", type=int, default=0,
                        help="只回放前 N 条记录（调试用）")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("-o", "--output", type=Path,
                        help="把完整结果写成 JSON")
    args = parser.parse_args(argv)
    args.zone_pages = args.zone_mib * 1024 * 1024 // args.page_size

    models = build_models(args)
    started = time.monotonic()
    meta = replay(args, models)
    elapsed = time.monotonic() - started

    reports = [m.report() for m in models]
    columns = ["model", "waf", "host_pages", "gc_pages", "gc_rounds",
               "stall_events", "invalid_by_free", "invalid_by_overwrite",
               "stream1_host_pages", "unique_slots"]
    widths = {c: max(len(c), 12) for c in columns}
    print()
    print("  ".join(c.ljust(widths[c]) for c in columns))
    for rep in reports:
        row = []
        for c in columns:
            value = rep[c]
            if isinstance(value, float):
                text = f"{value:.4f}"
            elif isinstance(value, int):
                text = f"{value:,}"
            else:
                text = str(value)
            row.append(text.ljust(widths[c]))
        print("  ".join(row))
    print(f"\n有效 SWAP_OUT 事件: {meta['effective_swapout_events']:,}, "
          f"耗时 {elapsed:.1f}s")
    print(f"几何: {args.data_zones} data + {args.op_zones} op zones, "
          f"zone={args.zone_mib}MiB ({args.zone_pages} pages), "
          f"GC trigger<={args.gc_trigger_free} stall<={args.gc_stall_free} "
          f"stop>={args.gc_stop_free} gc-stream={args.gc_stream}")

    if args.output:
        args.output.write_text(json.dumps({
            "args": {k: str(v) if isinstance(v, Path) else v
                     for k, v in vars(args).items()},
            "meta": meta,
            "models": reports,
        }, indent=2, ensure_ascii=False))
        print(f"结果已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
