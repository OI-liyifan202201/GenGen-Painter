import asyncio
import struct
import random
import time
import heapq
import logging
import threading
import numpy as np
from collections import deque
from typing import List, Tuple, Optional, Dict
from PIL import Image
import sys
import os
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------
# 修复：移除末尾空格（关键！）
# ---------------------------
API_BASE_URL = "https://paintboard.luogu.me"  # ← 已删除末尾空格！
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

COOLING_TIME = 30  # 冷却时间（秒）

# ---------------------------
# 优化版 AccountManager：精确冷却 + Token 缓存
# ---------------------------
class AccountManager:
    def __init__(self, credentials: List[Tuple[int, str]]):
        self.lock = threading.Lock()
        self.heap = []
        now = time.time()
        for uid, access_key in credentials:
            heapq.heappush(self.heap, (now, uid, access_key, None))

    def get_available_account(self) -> Optional[Dict]:
        with self.lock:
            if not self.heap:
                return None
            next_available, uid, access_key, token = self.heap[0]
            now = time.time()
            if now >= next_available:
                heapq.heappop(self.heap)
                return {"uid": uid, "access_key": access_key, "token": token}
            return None

    def release_account(self, uid: int, access_key: str, token: Optional[str]):
        with self.lock:
            next_available = time.time() + COOLING_TIME
            heapq.heappush(self.heap, (next_available, uid, access_key, token))

    def update_token(self, uid: int, token: str):
        with self.lock:
            new_heap = []
            for next_available, u, access_key, t in self.heap:
                if u == uid:
                    new_heap.append((next_available, u, access_key, token))
                else:
                    new_heap.append((next_available, u, access_key, t))
            heapq.heapify(new_heap)
            self.heap = new_heap


# ---------------------------
# PaintBoardClient：使用外部 session，禁止内部创建
# ---------------------------
class PaintBoardClient:
    def __init__(self, uid: int, access_key: str, connection_type="writeonly", account_manager=None):
        self.uid = uid
        self.access_key = access_key
        self.connection_type = connection_type
        self.account_manager = account_manager
        self.token = None
        self.websocket = None
        self.connected = False
        self.last_heartbeat = time.time()

    def _is_ws_open(self) -> bool:
        ws = self.websocket
        return ws is not None and not ws.closed

    async def get_token(self, session: aiohttp.ClientSession):
        try:
            url = f"{API_BASE_URL}/api/auth/gettoken"
            payload = {"uid": self.uid, "access_key": self.access_key}
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
                if result.get("code") == 200 and "data" in result:
                    self.token = result["data"]["token"]
                    if self.account_manager:
                        self.account_manager.update_token(self.uid, self.token)
                    logger.info(f"[{self.uid}] 获取新 Token 成功")
                    return True
                else:
                    logger.error(f"[{self.uid}] 获取 Token 失败: {result}")
                    return False
        except Exception as e:
            logger.error(f"[{self.uid}] 获取 Token 异常: {e}")
            return False

    async def connect_websocket(self, session: aiohttp.ClientSession):
        if not self.token:
            if not await self.get_token(session):
                return False
        try:
            url = WEBSOCKET_URL
            if self.connection_type == "readonly":
                url += "?readonly=1"
            elif self.connection_type == "writeonly":
                url += "?writeonly=1"

            ws = await session.ws_connect(url, timeout=10, autoclose=True)
            self.websocket = ws
            self.connected = True
            logger.info(f"[{self.uid}] WebSocket 连接成功")
            return True
        except Exception as e:
            logger.error(f"[{self.uid}] WebSocket 连接失败: {e}")
            self.connected = False
            self.websocket = None
            return False

    async def ensure_connected(self, session: aiohttp.ClientSession):
        if not self._is_ws_open():
            logger.info(f"[{self.uid}] WebSocket 断开，尝试重连...")
            return await self.connect_websocket(session)
        return True

    async def _ws_send_bytes(self, data: bytes):
        if not self._is_ws_open():
            raise RuntimeError("WebSocket closed")
        await self.websocket.send_bytes(data)

    async def send_heartbeat(self, session: aiohttp.ClientSession):
        """只发送心跳，不处理重连"""
        while True:
            try:
                if self._is_ws_open():
                    await self._ws_send_bytes(bytes([0xfb]))
                    self.last_heartbeat = time.time()
                # 不在此处重连！
            except Exception as e:
                logger.warning(f"[{self.uid}] 心跳异常: {e}")
                self.connected = False
                self.websocket = None
            await asyncio.sleep(10)

    async def send_paint_data(self, session: aiohttp.ClientSession, x, y, r, g, b):
        if not await self.ensure_connected(session):
            logger.warning(f"[{self.uid}] 无法连接 WebSocket，跳过绘制 ({x},{y})")
            return False
        if not self.token:
            logger.warning(f"[{self.uid}] 无有效 Token，跳过绘制")
            return False

        try:
            packet = bytearray()
            packet.append(0xfe)
            packet.extend(struct.pack('<H', x))
            packet.extend(struct.pack('<H', y))
            packet.extend([r, g, b])
            packet.extend(struct.pack('<I', self.uid)[:3])
            token_clean = self.token.replace('-', '')
            if len(token_clean) != 32:
                logger.error(f"[{self.uid}] Token 格式错误")
                return False
            token_bytes = bytes.fromhex(token_clean)
            packet.extend(token_bytes)
            packet.extend(struct.pack('<I', random.randint(0, 2**32-1)))
            await self._ws_send_bytes(bytes(packet))
            logger.debug(f"[{self.uid}] 绘制成功: ({x},{y}) RGB({r},{g},{b})")
            return True
        except Exception as e:
            logger.warning(f"[{self.uid}] 绘制失败 ({x},{y}): {e}，尝试刷新 Token")
            self.token = None
            return False


# ---------------------------
# WorkScheduler：全量差异扫描（无论 mode）
# ---------------------------
class WorkScheduler:
    def __init__(self, image_data, board, offset_x=0, offset_y=0):
        self.image_data = image_data
        self.height, self.width, _ = image_data.shape
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.work_queue = deque()
        self.total = 0
        self.done = 0
        self._build_diff_queue(board)

    def _build_diff_queue(self, board):
        pixels = []
        for y in range(self.height):
            for x in range(self.width):
                bx, by = x + self.offset_x, y + self.offset_y
                if bx >= 1000 or by >= 600:
                    continue
                if not np.array_equal(self.image_data[y, x], board[by, bx]):
                    pixels.append((x, y))
        self.work_queue = deque(pixels)
        self.total = len(pixels)
        logger.info(f"初始差异像素数: {self.total}")

    def get_next_batch(self, batch_size=1):
        batch = []
        for _ in range(batch_size):
            if not self.work_queue:
                break
            x, y = self.work_queue.popleft()
            r, g, b = self.image_data[y, x]
            batch.append((x + self.offset_x, y + self.offset_y, r, g, b))
            self.done += 1
        return batch

    def rebuild_from_full_scan(self, board):
        """全量重新扫描差异"""
        old_len = len(self.work_queue)
        self.work_queue.clear()
        for y in range(self.height):
            for x in range(self.width):
                bx, by = x + self.offset_x, y + self.offset_y
                if bx >= 1000 or by >= 600:
                    continue
                if not np.array_equal(self.image_data[y, x], board[by, bx]):
                    self.work_queue.append((x, y))
        new_len = len(self.work_queue)
        logger.info(f"全量修复扫描完成，发现 {new_len} 个差异像素（原队列: {old_len}）")
        self.total = max(self.total, self.done + new_len)


# ---------------------------
# ImagePainter：主绘制器
# ---------------------------
class ImagePainter:
    def __init__(self, image_path, offset_x=0, offset_y=0):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.account_manager = AccountManager(USER_CREDENTIALS)
        self.clients = [PaintBoardClient(uid, key, "writeonly", self.account_manager)
                        for uid, key in USER_CREDENTIALS]
        self.scheduler = None
        self.running = True

    def load_image(self):
        img = Image.open(self.image_path).convert("RGB")
        self.image_data = np.array(img)
        return True

    async def get_board_state(self, session: aiohttp.ClientSession):
        try:
            async with session.get(f"{API_BASE_URL}/api/paintboard/getboard") as resp:
                content = await resp.read()
                board = np.frombuffer(content, dtype=np.uint8).reshape((600, 1000, 3))
                return board
        except Exception as e:
            logger.error(f"获取画板状态失败: {e}")
            return None

    async def send_task(self, session: aiohttp.ClientSession):
        last_log_time = 0
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(1)
                continue

            account = self.account_manager.get_available_account()
            if not account:
                await asyncio.sleep(0.1)
                continue

            client = next((c for c in self.clients if c.uid == account['uid']), None)
            if not client:
                self.account_manager.release_account(account['uid'], account['access_key'], account.get('token'))
                continue

            batch = self.scheduler.get_next_batch(batch_size=1)
            if not batch:
                self.account_manager.release_account(account['uid'], account['access_key'], account.get('token'))
                if time.time() - last_log_time > 5:
                    progress = self.scheduler.done / self.scheduler.total if self.scheduler.total else 1
                    active = sum(1 for c in self.clients if c.connected)
                    # ✅ 严格按照指定格式输出
                    logger.info(
                        f"初始进度: {self.scheduler.done}/{self.scheduler.total} "
                        f"({progress*100:.1f}%) - 修复任务: {len(self.scheduler.work_queue)} "
                        f"- 活动账户: {active}/{len(self.clients)}"
                    )
                    last_log_time = time.time()
                await asyncio.sleep(0.2)
                continue

            success = await client.send_paint_data(session, *batch[0])
            self.account_manager.release_account(account['uid'], account['access_key'], client.token)

            if time.time() - last_log_time > 5:
                progress = self.scheduler.done / self.scheduler.total if self.scheduler.total else 1
                active = sum(1 for c in self.clients if c.connected)
                # ✅ 严格按照指定格式输出
                logger.info(
                    f"初始进度: {self.scheduler.done}/{self.scheduler.total} "
                    f"({progress*100:.1f}%) - 修复任务: {len(self.scheduler.work_queue)} "
                    f"- 活动账户: {active}/{len(self.clients)}"
                )
                last_log_time = time.time()

            await asyncio.sleep(0.05)

    async def check_and_repair(self, session: aiohttp.ClientSession):
        """每10秒全量扫描一次差异"""
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(1)
                continue
            try:
                board = await self.get_board_state(session)
                if board is None:
                    await asyncio.sleep(5)
                    continue
                self.scheduler.rebuild_from_full_scan(board)
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"全量修复扫描异常: {e}")
                await asyncio.sleep(5)

    async def run(self):
        if not self.load_image():
            return False

        async with aiohttp.ClientSession() as session:
            board = await self.get_board_state(session)
            if board is None:
                logger.error("无法获取初始画板状态")
                return False

            self.scheduler = WorkScheduler(self.image_data, board, self.offset_x, self.offset_y)

            tasks = []

            # 启动心跳任务（带延迟避免连接风暴）
            for i, client in enumerate(self.clients):
                await asyncio.sleep(0.2)  # 每隔 0.2 秒启动一个，避免 429
                tasks.append(asyncio.create_task(client.send_heartbeat(session)))

            tasks.append(asyncio.create_task(self.send_task(session)))
            tasks.append(asyncio.create_task(self.check_and_repair(session)))

            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass
            finally:
                self.running = False
                for t in tasks:
                    t.cancel()
        return True


def print_banner():
    print("=" * 60)
    print("                    GenGen Painter (极速全量修复版)")
    print("=" * 60)
    print("优化特性:")
    print(f"  • 账户数量: {len(USER_CREDENTIALS)} 个")
    print("  • 每账户冷却: 30秒/点")
    print("  • 全量差异扫描: 每10秒完整比对一次")
    print("  • Token 复用: 仅失效时刷新")
    print("  • 中文日志: 实时进度 + 修复详情")
    print("=" * 60)


def get_user_input():
    print_banner()
    image_path = r"c:\Users\admin\Desktop\result.jpeg"
    if not os.path.exists(image_path):
        print("默认路径文件不存在，请确保路径正确！")
        sys.exit(1)

    try:
        offset_x = int(input("请输入左上角X坐标偏移 (默认0): ") or "0")
        offset_y = int(input("请输入左上角Y坐标偏移 (默认0): ") or "0")
    except ValueError:
        offset_x, offset_y = 0, 0

    return image_path, offset_x, offset_y


async def main():
    image_path, offset_x, offset_y = get_user_input()
    painter = ImagePainter(image_path, offset_x, offset_y)
    print(f"\n开始绘制！使用 {len(USER_CREDENTIALS)} 个账户，每30秒/点")
    print("程序每10秒全量扫描一次差异，自动修复被覆盖像素")
    print("按 Ctrl+C 停止程序\n")
    try:
        await painter.run()
    except KeyboardInterrupt:
        print("\n程序已停止")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        offset_x = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        offset_y = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        painter = ImagePainter(image_path, offset_x, offset_y)
        asyncio.run(painter.run())
    else:
        asyncio.run(main())