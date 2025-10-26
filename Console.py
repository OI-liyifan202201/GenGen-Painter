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

# ---------------------------
# 配置（无多余空格）
# ---------------------------
API_BASE_URL = "https://paintboard.luogu.me"
WEBSOCKET_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

# ---------------------------
# 账号列表（34个）
# ---------------------------
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

# ---------------------------
# 账户冷却管理器
# ---------------------------
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
        next_time = time.time() + 30  # 30秒冷却
        heapq.heappush(self.heap, (next_time, uid, key))


# ---------------------------
# 简化客户端（自动重连 + transport 错误处理）
# ---------------------------
class SimpleClient:
    def __init__(self, uid: int, access_key: str):
        self.uid = uid
        self.access_key = access_key
        self.token = None
        self.ws = None
        self.session = None

    async def get_token(self):
        if self.token:
            return True
        try:
            url = f"{API_BASE_URL}/api/auth/gettoken"
            payload = {"uid": self.uid, "access_key": self.access_key}
            async with self.session.post(url, json=payload) as resp:
                data = await resp.json()
                if data.get("statusCode") == 200 and "data" in data:
                    self.token = data["data"]["token"]
                    return True
                else:
                    logger.error(f"[{self.uid}] 获取 token 失败: {data}")
        except Exception as e:
            logger.error(f"[{self.uid}] 获取 token 异常: {e}")
        return False

    async def connect_ws(self):
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
            self.ws = None
            if not await self.get_token():
                return False
            if not await self.connect_ws():
                return False
        return True

    async def send_pixel(self, x: int, y: int, r: int, g: int, b: int) -> bool:
        if self.ws is None or self.ws.closed:
            return False
        try:
            packet = bytearray([0xfe])
            packet.extend(struct.pack('<H', x))
            packet.extend(struct.pack('<H', y))
            packet.extend([r, g, b])
            packet.extend(struct.pack('<I', self.uid)[:3])
            token_clean = self.token.replace('-', '')
            if len(token_clean) != 32:
                logger.error(f"[{self.uid}] Token 长度错误: {len(token_clean)}")
                return False
            token_bytes = bytes.fromhex(token_clean)
            packet.extend(token_bytes)
            packet.extend(struct.pack('<I', random.randint(0, 2**32 - 1)))
            await self.ws.send_bytes(bytes(packet))
            return True
        except (RuntimeError, ConnectionError, OSError) as e:
            err_str = str(e)
            if "closing transport" in err_str or "Cannot write" in err_str or "broken pipe" in err_str.lower():
                logger.warning(f"[{self.uid}] WebSocket 连接已失效（closing transport），将重连")
            else:
                logger.warning(f"[{self.uid}] 连接异常: {e}")
            self.ws = None
            # 不清空 token，因为 token 通常仍有效；仅连接失效
            return False
        except Exception as e:
            logger.warning(f"[{self.uid}] 发送未知错误: {e}")
            self.ws = None
            return False


# ---------------------------
# 全量差异调度器（支持扫描线/随机）
# ---------------------------
class FullDiffScheduler:
    def __init__(self, target_image: np.ndarray, offset_x: int, offset_y: int, mode: str):
        self.target = target_image
        self.h, self.w = target_image.shape[:2]
        self.ox, self.oy = offset_x, offset_y
        self.mode = mode  # "scanline" or "random"
        self.total_pixels = self.h * self.w

    async def get_work_list(self, current_board: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
        diffs = []
        for y in range(self.h):
            for x in range(self.w):
                bx, by = x + self.ox, y + self.oy
                if bx >= 1000 or by >= 600:
                    continue
                expected = self.target[y, x]
                actual = current_board[by, bx]
                if not np.array_equal(expected, actual):
                    r, g, b = expected
                    diffs.append((bx, by, int(r), int(g), int(b)))
        if self.mode == "scanline":
            diffs.sort(key=lambda p: (p[1], p[0]))  # 按 y, x 排序
        elif self.mode == "random":
            random.shuffle(diffs)
        return diffs


# ---------------------------
# 主绘制器
# ---------------------------
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
        self.scheduler = FullDiffScheduler(self.target_image, self.offset_x, self.offset_y, self.mode)

    async def get_board(self, session: aiohttp.ClientSession) -> Optional[np.ndarray]:
        try:
            async with session.get(f"{API_BASE_URL}/api/paintboard/getboard") as resp:
                data = await resp.read()
                return np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
        except Exception as e:
            logger.error(f"获取画板失败: {e}")
            return None

    async def run(self):
        self.load_image()
        async with aiohttp.ClientSession() as session:
            last_log = 0
            while self.running:
                board = await self.get_board(session)
                if board is None:
                    await asyncio.sleep(5)
                    continue

                work_list = await self.scheduler.get_work_list(board)
                done = self.scheduler.total_pixels - len(work_list)
                total = self.scheduler.total_pixels
                progress_pct = (done / total * 100) if total > 0 else 100.0

                if not work_list:
                    if time.time() - last_log > 5:
                        active = sum(1 for c in self.clients.values() if c.ws and not c.ws.closed)
                        logger.info(f"初始进度: {done}/{total} ({progress_pct:.1f}%) - 修复任务: 0 - 活动账户: {active}/{len(USER_CREDENTIALS)}")
                        logger.info("绘制已完成！持续监控中...")
                        last_log = time.time()
                    await asyncio.sleep(5)
                    continue

                # 取第一个任务
                x, y, r, g, b = work_list[0]

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

                # 日志输出（每5秒）
                if time.time() - last_log > 5:
                    active = sum(1 for c in self.clients.values() if c.ws and not c.ws.closed)
                    logger.info(f"初始进度: {done}/{total} ({progress_pct:.1f}%) - 修复任务: {len(work_list)} - 活动账户: {active}/{len(USER_CREDENTIALS)}")
                    last_log = time.time()

                await asyncio.sleep(0.1)


# ---------------------------
# 启动入口
# ---------------------------
def main():
    if len(sys.argv) < 4:
        print("用法: python painter.py <图片路径> <X偏移> <Y偏移> [模式]")
        sys.exit(1)

    image_path = sys.argv[1]
    if not os.path.exists(image_path):
        print(f"图像文件不存在: {image_path}")
        sys.exit(1)

    try:
        offset_x = int(sys.argv[2])
        offset_y = int(sys.argv[3])
        mode = sys.argv[4] if len(sys.argv) > 4 else "1"
        if mode not in ("0", "1"):
            print("模式必须是 0 或 1")
            sys.exit(1)
    except ValueError as e:
        print(f"参数错误: {e}")
        sys.exit(1)

    print(f"\n启动绘制任务")
    print(f"图片: {image_path}")
    print(f"偏移: ({offset_x}, {offset_y})")
    print(f"模式: {'扫描线' if mode == '1' else '随机撒点'}")
    print(f"账号数: {len(USER_CREDENTIALS)}")
    print("按 Ctrl+C 停止\n")

    painter = FastPainter(image_path, offset_x, offset_y, mode)
    try:
        asyncio.run(painter.run())
    except KeyboardInterrupt:
        print("\n用户中断，程序退出")


if __name__ == "__main__":
    main()