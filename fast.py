import asyncio
import struct
import random
import time
import logging
import numpy as np
from typing import List, Tuple, Optional
from PIL import Image
import sys
import os
import aiohttp

# ---------------------------
# 配置
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

MAX_CONNECTIONS = 5          # 服务器限制：每个 IP 最多 5 连接
BATCH_SIZE = 1000           # 每包最多点数（30B/点 → 30KB < 32KB）
PACKAGE_MAX_BYTES = 32768   # 32KB 上限

# ---------------------------
# 全局任务队列（带 PPS 统计）
# ---------------------------
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.total = 0
        self.completed = 0
        self.lock = asyncio.Lock()
        self.recent_done = []  # [(timestamp, count), ...]

    async def put(self, task):
        await self.queue.put(task)
        async with self.lock:
            self.total += 1

    async def get_batch(self, size: int):
        batch = []
        for _ in range(size):
            try:
                task = self.queue.get_nowait()
                batch.append(task)
            except asyncio.QueueEmpty:
                break
        return batch

    async def mark_done(self, n: int):
        now = time.time()
        async with self.lock:
            self.completed += n
            self.recent_done.append((now, n))
            # 仅保留最近 10 秒数据
            self.recent_done = [(t, c) for t, c in self.recent_done if now - t <= 10]

    async def get_progress_and_pps(self):
        now = time.time()
        async with self.lock:
            done, total = self.completed, self.total
            # 计算最近 5 秒内的 PPS
            window = 5
            recent = [(t, c) for t, c in self.recent_done if now - t <= window]
            pps = sum(c for _, c in recent) / max((now - min((t for t, _ in recent), default=now) or 1), 1e-3)
            pps = min(pps, 10000)  # 防止初始 spike
            return done, total, pps

# ---------------------------
# 账号轮换器（Round-Robin）
# ---------------------------
class AccountRotator:
    def __init__(self, credentials: List[Tuple[int, str]]):
        self.accounts = [{"uid": uid, "access_key": ak} for uid, ak in credentials]
        self.index = 0
        self.lock = asyncio.Lock()

    async def get_next(self) -> dict:
        async with self.lock:
            acc = self.accounts[self.index]
            self.index = (self.index + 1) % len(self.accounts)
            return acc

# ---------------------------
# 客户端（支持粘包）
# ---------------------------
class PaintBoardClient:
    def __init__(self, uid: int, access_key: str):
        self.uid = uid
        self.access_key = access_key
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
                if resp.status == 200 and "data" in data and "token" in data["data"]:
                    self.token = data["data"]["token"]
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
            logger.debug(f"[{self.uid}] WebSocket 连接失败: {e}")
            return False

    def _make_packet(self, x: int, y: int, r: int, g: int, b: int) -> bytes:
        pkt = bytearray([0xfe])
        pkt.extend(struct.pack('<H', x))
        pkt.extend(struct.pack('<H', y))
        pkt.extend([r, g, b])
        pkt.extend(struct.pack('<I', self.uid)[:3])
        token_hex = self.token.replace('-', '')
        if len(token_hex) != 32:
            raise ValueError("Token 长度错误")
        pkt.extend(bytes.fromhex(token_hex))
        pkt.extend(struct.pack('<I', random.randint(0, 2**32 - 1)))
        return bytes(pkt)

    async def send_batch(self, tasks: List[Tuple[int, int, int, int, int]]) -> bool:
        if not self.websocket or self.websocket.closed:
            return False
        try:
            batch_pkt = bytearray()
            for x, y, r, g, b in tasks:
                pkt = self._make_packet(x, y, r, g, b)
                if len(batch_pkt) + len(pkt) > PACKAGE_MAX_BYTES:
                    break
                batch_pkt.extend(pkt)
            if batch_pkt:
                await self.websocket.send_bytes(batch_pkt)
            return True
        except Exception as e:
            logger.warning(f"[{self.uid}] 批量发送失败: {e}")
            return False

    async def close(self):
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()

# ---------------------------
# 主绘图器
# ---------------------------
class ImagePainter:
    def __init__(self, image_path: str, offset_x: int, offset_y: int, mode: str):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.mode = mode
        self.task_queue = TaskQueue()
        self.running = True
        self._last_log = 0

    def load_image(self) -> np.ndarray:
        img = Image.open(self.image_path).convert("RGB")
        return np.array(img)

    async def get_board(self, session: aiohttp.ClientSession) -> Optional[np.ndarray]:
        try:
            async with session.get(f"{API_BASE_URL}/api/paintboard/getboard") as resp:
                data = await resp.read()
                return np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
        except Exception as e:
            logger.error(f"获取画板失败: {e}")
            return None

    async def build_tasks(self, session: aiohttp.ClientSession):
        image = self.load_image()
        board = await self.get_board(session)
        if board is None:
            raise RuntimeError("无法获取初始画板")

        h, w = image.shape[:2]
        points = []
        for y in range(h):
            for x in range(w):
                bx, by = x + self.offset_x, y + self.offset_y
                if bx >= 1000 or by >= 600:
                    continue
                if not np.array_equal(image[y, x], board[by, bx]):
                    points.append((bx, by, int(image[y, x][0]), int(image[y, x][1]), int(image[y, x][2])))

        if self.mode == "random":
            random.shuffle(points)

        for p in points:
            await self.task_queue.put(p)

        logger.info(f"✅ 差异点加载完成: {len(points)} 个")

    async def worker(self, session: aiohttp.ClientSession, account_rotator: AccountRotator):
        while self.running:
            batch = await self.task_queue.get_batch(BATCH_SIZE)
            if not batch:
                await asyncio.sleep(0.1)
                continue

            acc = await account_rotator.get_next()
            client = PaintBoardClient(acc["uid"], acc["access_key"])
            success = False
            try:
                if await client.connect(session):
                    success = await client.send_batch(batch)
            except Exception as e:
                logger.warning(f"[{acc['uid']}] Worker 异常: {e}")
            finally:
                await client.close()

            if success:
                await self.task_queue.mark_done(len(batch))
            else:
                # 重入队
                for task in batch:
                    await self.task_queue.put(task)

            # 日志（每3秒）
            now = time.time()
            if now - self._last_log > 3:
                done, total, pps = await self.task_queue.get_progress_and_pps()
                pct = (done / total * 100) if total else 0
                logger.info(
                    f"进度: {done}/{total} ({pct:.1f}%) | "
                    f"速度: {pps:.1f} 点/秒 | "
                    f"连接数: {MAX_CONNECTIONS}"
                )
                self._last_log = now

    async def run(self):
        async with aiohttp.ClientSession() as session:
            await self.build_tasks(session)
            account_rotator = AccountRotator(USER_CREDENTIALS)
            workers = [self.worker(session, account_rotator) for _ in range(MAX_CONNECTIONS)]
            try:
                await asyncio.gather(*workers)
            except asyncio.CancelledError:
                pass
            finally:
                self.running = False

# ---------------------------
# 用户输入
# ---------------------------
def get_input():
    path = r"c:\Users\admin\Desktop\result.jpeg"
    if not os.path.exists(path):
        print("❌ 图片不存在，请确认路径！")
        sys.exit(1)

    try:
        x = int(input("X偏移 (默认0): ") or "0")
        y = int(input("Y偏移 (默认0): ") or "0")
        mode = "random" if input("模式 (0=扫描线,1=随机,默认0): ").strip() == "1" else "scanline"
        print(f"✅ 模式: {'随机撒点' if mode == 'random' else '扫描线'}")
        return path, x, y, mode
    except Exception as e:
        print(f"输入错误: {e}")
        sys.exit(1)

# ---------------------------
# 主函数
# ---------------------------
async def main():
    path, x, y, mode = get_input()
    painter = ImagePainter(path, x, y, mode)
    print(f"\n🚀 启动绘制（账号: {len(USER_CREDENTIALS)}，连接: {MAX_CONNECTIONS}）")
    print("按 Ctrl+C 停止\n")
    try:
        await painter.run()
    except KeyboardInterrupt:
        print("\n🛑 用户中断")

if __name__ == "__main__":
    asyncio.run(main())