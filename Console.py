import asyncio
import struct
import random
import time
import heapq
import logging
import numpy as np
from typing import List, Tuple, Optional
from PIL import Image
import sys
import os
import aiohttp

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

class AccountManager:
    def __init__(self, credentials: List[Tuple[int, str]]):
        self.heap = []
        now = time.time()
        for uid, key in credentials:
            heapq.heappush(self.heap, (now, uid, key))

    def get_available_account(self) -> Optional[Tuple[int, str]]:
        if not self.heap:
            return None
        next_time, uid, key = self.heap[0]
        if time.time() >= next_time:
            heapq.heappop(self.heap)
            return (uid, key)
        return None

    def release_account(self, uid: int, key: str):
        next_time = time.time() + 30
        heapq.heappush(self.heap, (next_time, uid, key))


class SimpleClient:
    def __init__(self, uid: int, access_key: str):
        self.uid = uid
        self.access_key = access_key
        self.token = None
        self.ws = None
        self.session = None

    async def get_token(self):
        url = f"{API_BASE_URL}/api/auth/gettoken"
        payload = {"uid": self.uid, "access_key": self.access_key}
        try:
            async with self.session.post(url, json=payload) as resp:
                data = await resp.json()
                if data.get("status") == 200 and "data" in data:
                    self.token = data["data"]["token"]
                    return True
        except Exception as e:
            logger.error(f"[{self.uid}] 获取 token 失败: {e}")
        return False

    async def connect_ws(self):
        if not self.token:
            if not await self.get_token():
                return False
        try:
            url = f"{WEBSOCKET_URL}?writeonly=1"
            self.ws = await self.session.ws_connect(url)
            return True
        except Exception as e:
            logger.error(f"[{self.uid}] WebSocket 连接失败: {e}")
            return False

    async def ensure_ready(self, session: aiohttp.ClientSession):
        self.session = session
        if self.ws is None or self.ws.closed:
            if not await self.connect_ws():
                return False
        return True

    async def send_pixel(self, x: int, y: int, r: int, g: int, b: int) -> bool:
        if not self.token:
            return False
        try:
            packet = bytearray([0xfe])
            packet.extend(struct.pack('<H', x))
            packet.extend(struct.pack('<H', y))
            packet.extend([r, g, b])
            packet.extend(struct.pack('<I', self.uid)[:3])
            token_clean = self.token.replace('-', '')
            if len(token_clean) != 32:
                return False
            token_bytes = bytes.fromhex(token_clean)
            packet.extend(token_bytes)
            packet.extend(struct.pack('<I', random.randint(0, 2**32 - 1)))
            await self.ws.send_bytes(bytes(packet))
            return True
        except Exception as e:
            logger.warning(f"[{self.uid}] 发送失败 ({x},{y}): {e}")
            self.ws = None
            return False


class FullWorkScheduler:
    def __init__(self, target_image: np.ndarray, offset_x: int, offset_y: int, mode: str):
        self.target = target_image
        self.h, self.w = target_image.shape[:2]
        self.ox, self.oy = offset_x, offset_y
        self.mode = mode  # 'scanline' or 'random'

    async def get_next_pixel(self, current_board: np.ndarray) -> Optional[Tuple[int, int, int, int, int]]:
        diff_pixels = []
        for y in range(self.h):
            for x in range(self.w):
                bx, by = x + self.ox, y + self.oy
                if bx >= 1000 or by >= 600:
                    continue
                expected = self.target[y, x]
                actual = current_board[by, bx]
                if not np.array_equal(expected, actual):
                    r, g, b = expected
                    diff_pixels.append((bx, by, int(r), int(g), int(b)))

        if not diff_pixels:
            return None

        if self.mode == "scanline":
            # 返回第一个（顺序）
            return diff_pixels[0]
        else:
            # 随机
            return random.choice(diff_pixels)

    def total_pixels(self) -> int:
        return self.h * self.w


class FastPainter:
    def __init__(self, image_path: str, offset_x: int, offset_y: int, mode: str):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.mode = "scanline" if mode == "1" else "random"
        self.account_manager = AccountManager(USER_CREDENTIALS)
        self.clients = {uid: SimpleClient(uid, key) for uid, key in USER_CREDENTIALS}
        self.target_image = None
        self.scheduler = None
        self.running = True

    def load_image(self):
        img = Image.open(self.image_path).convert("RGB")
        self.target_image = np.array(img)
        self.scheduler = FullWorkScheduler(self.target_image, self.offset_x, self.offset_y, self.mode)

    async def get_board(self, session: aiohttp.ClientSession) -> Optional[np.ndarray]:
        try:
            async with session.get(f"{API_BASE_URL}/api/paintboard/getboard") as resp:
                data = await resp.read()
                return np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
        except Exception as e:
            logger.error(f"获取画板失败: {e}")
            return None

    async def paint_loop(self, session: aiohttp.ClientSession):
        last_log = 0
        total = self.scheduler.total_pixels()
        while self.running:
            board = await self.get_board(session)
            if board is None:
                await asyncio.sleep(5)
                continue

            pixel = await self.scheduler.get_next_pixel(board)
            if pixel is None:
                if time.time() - last_log > 5:
                    logger.info("绘制完成！所有像素已正确。")
                    last_log = time.time()
                await asyncio.sleep(5)
                continue

            x, y, r, g, b = pixel

            account = self.account_manager.get_available_account()
            if not account:
                await asyncio.sleep(0.1)
                continue

            uid, key = account
            client = self.clients[uid]

            if not await client.ensure_ready(session):
                self.account_manager.release_account(uid, key)
                await asyncio.sleep(0.5)
                continue

            success = await client.send_pixel(x, y, r, g, b)
            self.account_manager.release_account(uid, key)

            if time.time() - last_log > 5:
                # 重新获取差异数用于日志（轻量估算）
                temp_diff = 0
                for dy in range(self.scheduler.h):
                    for dx in range(self.scheduler.w):
                        bx, by = dx + self.offset_x, dy + self.offset_y
                        if bx < 1000 and by < 600:
                            if not np.array_equal(self.target_image[dy, dx], board[by, bx]):
                                temp_diff += 1
                done = total - temp_diff
                pct = done / total * 100 if total else 100
                logger.info(f"进度: {done}/{total} ({pct:.1f}%) - 差异像素: {temp_diff}")
                last_log = time.time()

            await asyncio.sleep(0.1)

    async def run(self):
        self.load_image()
        async with aiohttp.ClientSession() as session:
            try:
                await self.paint_loop(session)
            except KeyboardInterrupt:
                self.running = False
                logger.info("程序已停止")


def main():
    if len(sys.argv) >= 2:
        image_path = sys.argv[1]
        offset_x = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        offset_y = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        mode = sys.argv[4] if len(sys.argv) > 4 else "1"
    else:
        print("GenGen Painter (最快全量版)")
        image_path = input("请输入图像路径: ").strip()
        if not os.path.exists(image_path):
            print("文件不存在")
            sys.exit(1)
        try:
            offset_x = int(input("X 偏移 (默认0): ") or "0")
            offset_y = int(input("Y 偏移 (默认0): ") or "0")
            mode = input("模式 (0=扫描线, 1=随机, 默认1): ").strip() or "1"
            if mode=="1":
                mode="2"
            else:
                mode="1"
        except Exception:
            offset_x = offset_y = 0
            mode = "1"

    if mode not in ("1", "2"):
        mode = "1"

    mode_name = "扫描线" if mode == "1" else "随机撒点"
    print(f"\n启动 {mode_name} 模式（全量差异检测，30秒/点）")
    print(f"账户数: {len(USER_CREDENTIALS)} | 偏移: ({offset_x}, {offset_y})")
    print("按 Ctrl+C 停止\n")

    painter = FastPainter(image_path, offset_x, offset_y, mode)
    try:
        asyncio.run(painter.run())
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()


