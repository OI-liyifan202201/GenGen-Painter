#!/usr/bin/env python3
import asyncio
import aiohttp
from PIL import Image
import os
import sys
import signal
from pathlib import Path
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.logging import RichHandler
import logging
import argparse

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
logger = logging.getLogger("paintboard")

# ===== 配置 =====
API_URL = "https://paintboard.luogu.me"
WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"
BOARD_WIDTH, BOARD_HEIGHT = 1000, 600
THREAD_COUNT = 7
RETRY_DELAY = 2.0  # 冷却重试延迟（秒）

# 预设账户（取前7个）
PRESET_ACCOUNTS = [
    {"uid": 661094,  "key": "lDrv8W9u"},
    {"uid": 661913,  "key": "lFT03zMS"},
    {"uid": 1351126, "key": "UJUVuzyk"},
    {"uid": 1032267, "key": "6XF2wDhG"},
    {"uid": 1404345, "key": "dJvxSGv6"},
    {"uid": 1036010, "key": "hcB8wQzm"},
    {"uid": 703022,  "key": "gJNV9lrN"},
]

class PaintWorker:
    def __init__(self, uid, access_key, thread_id):
        self.uid = uid
        self.access_key = access_key
        self.thread_id = thread_id
        self.token = None
        self.ws = None
        self.session = None
        self.queue = asyncio.Queue()
        self.paint_id = 0
        self.running = False
        self.stats = {"sent": 0, "failed": 0, "retry": 0}

    async def get_token(self):
        async with self.session.post(
            f"{API_URL}/api/auth/gettoken",
            json={"uid": self.uid, "access_key": self.access_key}
        ) as resp:
            data = await resp.json()
            if data.get("data", {}).get("token"):
                self.token = data["data"]["token"]
                logger.info(f"[线程 {self.thread_id}] Token 获取成功")
                return True
            else:
                logger.error(f"[线程 {self.thread_id}] Token 失败: {data.get('data', {}).get('errorType', 'Unknown')}")
                return False

    async def connect_paintboard(self):
        self.ws = await self.session.ws_connect(WS_URL)
        logger.info(f"[线程 {self.thread_id}] 连接 Paintboard 成功")

    async def send_paint(self, x, y, r, g, b):
        if not self.token or not self.ws:
            return False
        
        packet = bytearray([0xfe])
        packet.extend(x.to_bytes(2, 'little'))
        packet.extend(y.to_bytes(2, 'little'))
        packet.extend([r, g, b])
        packet.extend(self.uid.to_bytes(3, 'little'))
        token_clean = self.token.replace('-', '')
        packet.extend(bytes.fromhex(token_clean))
        packet.extend((self.paint_id).to_bytes(4, 'little'))
        self.paint_id = (self.paint_id + 1) % (2**32)
        
        await self.ws.send_bytes(packet)
        self.stats["sent"] += 1
        return True

    async def run(self, session, global_state):
        self.session = session
        if not await self.get_token():
            return
        
        await self.connect_paintboard()
        self.running = True
        
        while self.running and global_state["running"]:
            try:
                # 动态负载均衡：如果队列空，尝试从其他线程偷任务
                if self.queue.empty():
                    for other in global_state["workers"]:
                        if other != self and not other.queue.empty():
                            try:
                                pixel = other.queue.get_nowait()
                                await self.queue.put(pixel)
                                break
                            except:
                                continue
                
                pixel = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                success = await self.send_paint(pixel['x'], pixel['y'], pixel['r'], pixel['g'], pixel['b'])
                if not success:
                    self.stats["failed"] += 1
                await asyncio.sleep(0.01)  # 10ms 粘包
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"[线程 {self.thread_id}] 错误: {e}")
                self.stats["failed"] += 1

    def stop(self):
        self.running = False

class PaintManager:
    def __init__(self, image_path, offset_x=0, offset_y=0):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.workers = []
        self.session = None
        self.board_data = None
        self.running = True
        self.total_pixels = 0
        self.painted_pixels = 0

    async def init_workers(self):
        self.session = aiohttp.ClientSession()
        self.workers = [
            PaintWorker(acc['uid'], acc['key'], i)
            for i, acc in enumerate(PRESET_ACCOUNTS[:THREAD_COUNT])
        ]

    async def fetch_board(self):
        logger.info("获取当前画板...")
        async with self.session.get(f"{API_URL}/api/paintboard/getboard") as resp:
            self.board_data = await resp.read()
        logger.info("画板获取完成")

    async def build_queues(self):
        # 加载目标图像
        img = Image.open(self.image_path).convert("RGB")
        img_w, img_h = img.size
        target_pixels = list(img.getdata())
        board = self.board_data

        # 清空队列
        for w in self.workers:
            while not w.queue.empty():
                try:
                    w.queue.get_nowait()
                except:
                    break

        # 分配任务
        block_h = BOARD_HEIGHT // len(self.workers)
        total = 0
        for y in range(BOARD_HEIGHT):
            worker_idx = min(y // block_h, len(self.workers) - 1)
            for x in range(BOARD_WIDTH):
                tx = x - self.offset_x
                ty = y - self.offset_y
                if 0 <= tx < img_w and 0 <= ty < img_h:
                    r, g, b = target_pixels[ty * img_w + tx]
                    board_idx = (y * BOARD_WIDTH + x) * 3
                    br, bg, bb = board[board_idx], board[board_idx+1], board[board_idx+2]
                    if (br, bg, bb) != (r, g, b):
                        await self.workers[worker_idx].queue.put({
                            'x': x, 'y': y, 'r': r, 'g': g, 'b': b
                        })
                        total += 1
        self.total_pixels = total
        logger.info(f"共需绘制 {total} 像素")

    async def start(self):
        await self.init_workers()
        await self.fetch_board()
        await self.build_queues()
        
        global_state = {
            "workers": self.workers,
            "running": True
        }
        
        # 启动所有 worker
        tasks = [w.run(self.session, global_state) for w in self.workers]
        await asyncio.gather(*tasks)

    def stop(self):
        self.running = False
        for w in self.workers:
            w.stop()

    def get_progress(self):
        # 简化：假设所有像素最终都会被绘制
        # 实际进度 = 已发送数 / 总像素（近似）
        sent = sum(w.stats["sent"] for w in self.workers)
        return min(sent, self.total_pixels), self.total_pixels

def make_layout():
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=3)
    )
    layout["main"].split_row(
        Layout(name="progress", ratio=2),
        Layout(name="stats", ratio=1)
    )
    return layout

def make_header():
    return Panel(Text("❄️ LGS Paintboard 2026 - 纯 Python TUI 版", justify="center", style="bold cyan"))

def make_footer(paused):
    status = "⏸️ 已暂停" if paused else "▶️ 运行中"
    return Panel(Text(f"状态: {status} | 按 [空格] 暂停/继续 | 按 Q 退出", style="green"))

def make_progress_table(manager, paused):
    if not manager:
        return Panel("等待初始化...", title="进度")
    
    done, total = manager.get_progress()
    pct = (done / total * 100) if total > 0 else 0
    
    table = Table(title=f"绘制进度: {done}/{total} ({pct:.1f}%)", expand=True)
    table.add_column("线程", style="cyan")
    table.add_column("已发送", style="green")
    table.add_column("失败", style="red")
    table.add_column("队列", style="yellow")
    
    for i, w in enumerate(manager.workers):
        queue_size = w.queue.qsize()
        table.add_row(
            f"线程 {i}",
            str(w.stats["sent"]),
            str(w.stats["failed"]),
            str(queue_size)
        )
    
    return table

async def keyboard_listener(manager, paused_event):
    """监听键盘事件（Windows/Linux）"""
    try:
        import keyboard
        while True:
            event = await asyncio.get_event_loop().run_in_executor(None, keyboard.read_event)
            if event.event_type == keyboard.KEY_DOWN:
                if event.name == 'space':
                    if paused_event.is_set():
                        paused_event.clear()
                        logger.info("▶️ 继续绘制")
                    else:
                        paused_event.set()
                        logger.info("⏸️ 暂停绘制")
                elif event.name.lower() == 'q':
                    logger.info("⏹️ 收到退出信号")
                    manager.stop()
                    break
    except Exception as e:
        logger.warning(f"键盘监听不可用（{e}），请手动 Ctrl+C 退出")

async def main():
    parser = argparse.ArgumentParser(description="LGS Paintboard 2026 自动绘画工具")
    parser.add_argument("image", help="目标图片路径")
    parser.add_argument("--offset-x", type=int, default=0, help="X 偏移 (默认: 0)")
    parser.add_argument("--offset-y", type=int, default=0, help="Y 偏移 (默认: 0)")
    args = parser.parse_args()

    if not Path(args.image).exists():
        logger.error(f"图片不存在: {args.image}")
        sys.exit(1)

    console = Console()
    layout = make_layout()
    layout["header"].update(make_header())
    
    manager = PaintManager(args.image, args.offset_x, args.offset_y)
    paused_event = asyncio.Event()  # 未设置 = 运行中

    # 启动键盘监听（可选）
    asyncio.create_task(keyboard_listener(manager, paused_event))

    # 启动绘制任务
    draw_task = asyncio.create_task(manager.start())

    with Live(layout, console=console, refresh_per_second=4) as live:
        try:
            while not draw_task.done():
                layout["footer"].update(make_footer(paused_event.is_set()))
                layout["progress"].update(make_progress_table(manager, paused_event.is_set()))
                await asyncio.sleep(0.25)
                
                # 如果暂停，等待事件
                if paused_event.is_set():
                    await asyncio.sleep(0.1)
                    
        except KeyboardInterrupt:
            logger.info("⏹️ 收到中断信号")
        finally:
            manager.stop()
            await draw_task
            if manager.session:
                await manager.session.close()

    logger.info("✅ 绘制任务结束")

if __name__ == "__main__":
    # 处理 Ctrl+C
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    asyncio.run(main())
