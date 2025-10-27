import asyncio
import struct
import random
import time
import heapq
import logging
import numpy as np
from collections import deque
from typing import List, Tuple, Optional, Dict
from PIL import Image
import sys
import os
import aiohttp

# ---------------------------
# 日志与配置
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

API_BASE_URL = "https://paintboard.luogu.me"
WEBSOCKET_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

USER_CREDENTIALS = [
    (661094, "lDrv8W9u"), (661913, "lFT03zMS"), (1351126, "UJUVuzyk"),
    (1032267, "6XF2wDhG"), (1404345, "dJvxSGv6"), (1036010, "hcB8wQzm"),
    (703022, "gJNV9lrN"), (1406692, "0WMtD3G7"), (1058607, "iyuq7QA2"),
    (1276209, "vzciwZs7"), (1227240, "WwnnjHVP"), (1406674, "NtqPbU8t"),
    (661984, "3BRDNLh0"), (1038207, "s3Cp6arh"),
    (1114894,"AKOWVHhq"),
    (964876,"K5YD3T6D"),
    (1227525,"KRXAngG0"),
    (1353086,"8bkVmyr4"),
    (1271532,"D9oYJsR4"),
    (556851,"Sup3mJwj"),
    (985515,"4TQ2uSNY"),
    (556676,"TbkdS4RX"),
    (911833,"QFc8hfZm"),
    (1646931,"ZJ8uDehZ"),
    (717599,"cHpWkkmz"),
    (748239,"0hjUxOD0"),
    (1261083,"uZxT0hps"),
    (681755,"gdeHd93f"),
    (1041338,"3rHIwBtw"),
    (1747411,"MINOqrED"),
    (1021053,"qE5hxvv9"),
    (1890667,"Ew40x77v"),
    (774851,"AenNc8Ln"),
    (1035756,"Iyfsiylq")
]

MAX_CONCURRENT = 5
COOLING_TIME = 30


# ---------------------------
# 账号管理器
# ---------------------------
class AccountManager:
    def __init__(self, credentials: List[Tuple[int, str]]):
        self.heap = []
        now = time.time()
        for uid, access_key in credentials:
            heapq.heappush(self.heap, (now, uid, access_key, None))

    def get_available_account(self) -> Optional[Dict]:
        if not self.heap:
            return None
        next_available, uid, access_key, token = self.heap[0]
        now = time.time()
        if now >= next_available:
            heapq.heappop(self.heap)
            return {"uid": uid, "access_key": access_key, "token": token}
        return None

    def release_account(self, uid: int, access_key: str, token: Optional[str]):
        next_available = time.time() + COOLING_TIME
        heapq.heappush(self.heap, (next_available, uid, access_key, token))

    def update_token(self, uid: int, token: str):
        new_heap = []
        for next_available, u, access_key, t in self.heap:
            if u == uid:
                new_heap.append((next_available, u, access_key, token))
            else:
                new_heap.append((next_available, u, access_key, t))
        heapq.heapify(new_heap)
        self.heap = new_heap


# ---------------------------
# 绘图客户端
# ---------------------------
class PaintBoardClient:
    def __init__(self, uid: int, access_key: str, account_manager: AccountManager):
        self.uid = uid
        self.access_key = access_key
        self.account_manager = account_manager
        self.token = None
        self.websocket = None

    async def get_token(self, session: aiohttp.ClientSession) -> bool:
        if self.token:
            return True
        try:
            async with session.post(
                f"{API_BASE_URL}/api/auth/gettoken",
                json={"uid": self.uid, "access_key": self.access_key}
            ) as resp:
                data = await resp.json()
                if "data" in data and "token" in data["data"]:
                    self.token = data["data"]["token"]
                    self.account_manager.update_token(self.uid, self.token)
                    return True
                logger.error(f"[{self.uid}] 获取 token 失败: {data}")
                return False
        except Exception as e:
            logger.error(f"[{self.uid}] 获取 token 异常: {e}")
            return False

    async def connect(self, session: aiohttp.ClientSession) -> bool:
        if not await self.get_token(session):
            return False
        try:
            url = f"{WEBSOCKET_URL}?writeonly=1"
            self.websocket = await session.ws_connect(url)
            return True
        except Exception as e:
            logger.debug(f"[{self.uid}] 连接失败: {e}")
            return False

    async def send_paint(self, x: int, y: int, r: int, g: int, b: int) -> bool:
        if not self.websocket or self.websocket.closed:
            return False
        try:
            pkt = bytearray([0xfe])
            pkt.extend(struct.pack('<H', x))
            pkt.extend(struct.pack('<H', y))
            pkt.extend([r, g, b])
            pkt.extend(struct.pack('<I', self.uid)[:3])
            token_hex = self.token.replace('-', '')
            if len(token_hex) != 32:
                logger.error(f"[{self.uid}] Token 长度错误")
                return False
            pkt.extend(bytes.fromhex(token_hex))
            pkt.extend(struct.pack('<I', random.randint(0, 2**32 - 1)))
            await self.websocket.send_bytes(bytes(pkt))
            return True
        except Exception as e:
            logger.warning(f"[{self.uid}] 绘制失败 ({x},{y}): {e}")
            return False

    async def close(self):
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()


# ---------------------------
# 任务调度器（修复进度清零问题）
# ---------------------------
class WorkScheduler:
    def __init__(self, image_data: np.ndarray, board: np.ndarray, offset_x: int, offset_y: int):
        self.image_data = image_data
        self.height, self.width = image_data.shape[:2]
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.initial_total = 0
        self.work_queue = deque()
        self._rebuild(board, first_time=True)

    def _rebuild(self, board: np.ndarray, first_time=False):
        new_queue = deque()
        for y in range(self.height):
            for x in range(self.width):
                bx, by = x + self.offset_x, y + self.offset_y
                if bx >= 1000 or by >= 600:
                    continue
                if not np.array_equal(self.image_data[y, x], board[by, bx]):
                    new_queue.append((x, y))
        if first_time:
            self.initial_total = len(new_queue)
        self.work_queue = new_queue
        remaining = len(new_queue)
        logger.info(f"✅ 全量检测完成，剩余修复: {remaining} / 初始差异: {self.initial_total}")

    def get_next(self) -> Optional[Tuple[int, int, int, int, int]]:
        if not self.work_queue:
            return None
        x, y = self.work_queue.popleft()
        r, g, b = self.image_data[y, x]
        return (x + self.offset_x, y + self.offset_y, int(r), int(g), int(b))

    def requeue(self, x_img: int, y_img: int):
        self.work_queue.appendleft((x_img, y_img))


# ---------------------------
# 主绘图器
# ---------------------------
class ImagePainter:
    def __init__(self, image_path: str, offset_x: int, offset_y: int, mode: int):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.mode = "scanline" if mode == 0 else "random"
        self.account_manager = AccountManager(USER_CREDENTIALS)
        self.scheduler = None
        self.running = True
        self.active_workers = 0
        self._lock = asyncio.Lock()
        self._last_log = 0

    def load_image(self) -> bool:
        try:
            img = Image.open(self.image_path).convert("RGB")
            self.image_data = np.array(img)
            return True
        except Exception as e:
            logger.error(f"加载图片失败: {e}")
            return False

    async def get_board(self, session: aiohttp.ClientSession) -> Optional[np.ndarray]:
        try:
            async with session.get(f"{API_BASE_URL}/api/paintboard/getboard") as resp:
                data = await resp.read()
                return np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
        except Exception as e:
            logger.error(f"获取画板失败: {e}")
            return None

    async def worker(self, session: aiohttp.ClientSession, sem: asyncio.Semaphore):
        while self.running:
            acc = self.account_manager.get_available_account()
            if not acc:
                await asyncio.sleep(0.3)
                continue

            async with sem:
                async with self._lock:
                    self.active_workers += 1
                    active_now = self.active_workers

                try:
                    client = PaintBoardClient(acc['uid'], acc['access_key'], self.account_manager)
                    if not await client.connect(session):
                        self.account_manager.release_account(acc['uid'], acc['access_key'], None)
                        continue

                    if not self.scheduler:
                        self.account_manager.release_account(acc['uid'], acc['access_key'], client.token)
                        await client.close()
                        continue

                    task = self.scheduler.get_next()
                    if not task:
                        self.account_manager.release_account(acc['uid'], acc['access_key'], client.token)
                        await client.close()
                        continue

                    success = await client.send_paint(*task)
                    if not success:
                        x_img = task[0] - self.offset_x
                        y_img = task[1] - self.offset_y
                        self.scheduler.requeue(x_img, y_img)

                    self.account_manager.release_account(acc['uid'], acc['access_key'], client.token)
                    await client.close()

                    # 打印进度（每5秒）
                    now = time.time()
                    if now - self._last_log > 5 and self.scheduler:
                        remaining = len(self.scheduler.work_queue)
                        fixed = self.scheduler.initial_total - remaining
                        total = self.scheduler.initial_total or 1
                        pct = (fixed / total) * 100
                        logger.info(
                            f"进度: {fixed}/{total} ({pct:.1f}%) "
                            f"- 修复任务: {remaining} "
                            f"- 活动账户: {active_now}/{len(USER_CREDENTIALS)}"
                        )
                        self._last_log = now

                finally:
                    async with self._lock:
                        self.active_workers -= 1

    async def full_check(self, session: aiohttp.ClientSession):
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(2)
                continue
            logger.info("🔍 开始全量差异检测...")
            board = await self.get_board(session)
            if board is not None:
                self.scheduler._rebuild(board, first_time=False)
            await asyncio.sleep(10)

    async def run(self) -> bool:
        if not self.load_image():
            return False

        async with aiohttp.ClientSession() as session:
            board = await self.get_board(session)
            if board is None:
                logger.error("无法获取初始画板")
                return False

            self.scheduler = WorkScheduler(self.image_data, board, self.offset_x, self.offset_y)

            sem = asyncio.Semaphore(MAX_CONCURRENT)
            tasks = [
                asyncio.create_task(self.full_check(session)),
                *[asyncio.create_task(self.worker(session, sem)) for _ in range(10)]
            ]

            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass
            finally:
                self.running = False
                for t in tasks:
                    t.cancel()
        return True


# ---------------------------
# 用户交互
# ---------------------------
def print_banner():
    print("=" * 60)
    print("           GenGen Painter (Python 3.13 · 进度修复版)")
    print("=" * 60)
    print(f"• 账号数: {len(USER_CREDENTIALS)}")
    print(f"• 并发: {MAX_CONCURRENT}")
    print("• 模式: 0=扫描线, 1=随机撒点")
    print("=" * 60)


def get_input():
    print_banner()
    path = r"c:\Users\admin\Desktop\result.jpeg"
    if not os.path.exists(path):
        print("❌ 图片不存在，请确认路径！")
        sys.exit(1)

    try:
        x = int(input("X偏移 (默认0): ") or "0")
        y = int(input("Y偏移 (默认0): ") or "0")
        mode = int(input("模式 (0=扫描线,1=随机,默认0): ") or "0")
        mode = 0 if mode not in (0, 1) else mode
        print(f"✅ 模式: {'扫描线' if mode == 0 else '随机撒点'}")
        return path, x, y, mode
    except Exception:
        return path, 0, 0, 0


# ---------------------------
# 主函数
# ---------------------------
async def main():
    path, x, y, mode = get_input()
    painter = ImagePainter(path, x, y, mode)
    print("\n🚀 开始绘制...")
    print(f"并发: {MAX_CONCURRENT}, 账号: {len(USER_CREDENTIALS)}")
    print("按 Ctrl+C 停止\n")
    try:
        await painter.run()
    except KeyboardInterrupt:
        print("\n🛑 中断退出")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = sys.argv[1]
        x = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        y = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        mode = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        mode = 0 if mode not in (0, 1) else mode
        asyncio.run(ImagePainter(path, x, y, mode).run())
    else:
        asyncio.run(main())