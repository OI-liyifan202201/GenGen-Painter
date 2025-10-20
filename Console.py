import asyncio
import websockets
import requests
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

API_BASE_URL = "https://paintboard.luogu.me"
WEBSOCKET_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

USER_CREDENTIALS = [
    (661094, "lDrv8W9u"), (661913, "lFT03zMS"), (1351126, "UJUVuzyk"),
    (1032267, "6XF2wDhG"), (1404345, "dJvxSGv6"), (1036010, "hcB8wQzm"),
    (703022, "gJNV9lrN"), (1406692, "0WMtD3G7"), (1058607, "iyuq7QA2"),
    (1276209, "vzciwZs7"), (1227240, "WwnnjHVP"), (1406674, "NtqPbU8t"),
    (661984, "3BRDNLh0"), (1038207, "s3Cp6arh")
]

# ---------------------------
# 优先队列版 AccountManager
# ---------------------------
class AccountManager:
    def __init__(self, credentials: List[Tuple[int, str]]):
        self.lock = threading.Lock()
        self.cooling_time = 30
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
            next_available = time.time() + self.cooling_time
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
# PaintBoardClient (心跳分离)
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

    async def get_token(self):
        try:
            url = f"{API_BASE_URL}/api/auth/gettoken"
            payload = {"uid": self.uid, "access_key": self.access_key}
            response = requests.post(url, json=payload)
            result = response.json()
            if "data" in result:
                self.token = result["data"]["token"]
                if self.account_manager:
                    self.account_manager.update_token(self.uid, self.token)
                return True
            return False
        except Exception as e:
            logger.error(f"获取 token 失败: {e}")
            return False

    async def connect_websocket(self):
        if not self.token:
            if not await self.get_token():
                return False
        try:
            url = WEBSOCKET_URL
            if self.connection_type == "readonly":
                url += "?readonly=1"
            elif self.connection_type == "writeonly":
                url += "?writeonly=1"
            self.websocket = await websockets.connect(url)
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"连接失败: {e}")
            self.connected = False
            return False

    async def ensure_connected(self):
        if not self.connected or self.websocket is None:
            return await self.connect_websocket()
        return True

    async def send_heartbeat(self):
        while True:
            try:
                if await self.ensure_connected() and self.websocket:
                    await self.websocket.send(bytes([0xfb]))
                    self.last_heartbeat = time.time()
            except Exception as e:
                logger.error(f"心跳失败: {e}")
                self.connected = False
            await asyncio.sleep(10)  # 固定心跳间隔

    async def send_paint_data(self, x, y, r, g, b):
        if not await self.ensure_connected() or self.token is None:
            return None
        try:
            packet = bytearray()
            packet.append(0xfe)
            packet.extend(struct.pack('<H', x))
            packet.extend(struct.pack('<H', y))
            packet.extend([r, g, b])
            packet.extend(struct.pack('<I', self.uid)[:3])
            token_clean = self.token.replace('-', '')
            token_bytes = bytes.fromhex(token_clean)
            packet.extend(token_bytes)
            packet.extend(struct.pack('<I', random.randint(0, 2**32-1)))
            await self.websocket.send(bytes(packet))
            return True
        except Exception as e:
            logger.error(f"绘制失败: {e}")
            return None

# ---------------------------
# WorkScheduler (动态采样率)
# ---------------------------
class WorkScheduler:
    def __init__(self, image_data, board, offset_x=0, offset_y=0):
        self.image_data = image_data
        self.height, self.width, _ = image_data.shape
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.work_queue = deque()
        for y in range(self.height):
            for x in range(self.width):
                bx, by = x + offset_x, y + offset_y
                if bx >= 1000 or by >= 600:
                    continue
                if not np.array_equal(image_data[y, x], board[by, bx]):
                    self.work_queue.append((x, y))
        self.total = len(self.work_queue)
        self.done = 0

    def get_next_batch(self, batch_size=3):
        batch = []
        for _ in range(batch_size):
            if not self.work_queue:
                break
            x, y = self.work_queue.popleft()
            r, g, b = self.image_data[y, x]
            batch.append((x+self.offset_x, y+self.offset_y, r, g, b))
            self.done += 1
        return batch

    def dynamic_sample_rate(self):
        progress = self.done / self.total if self.total else 1
        if progress < 0.3:
            return 0.05
        elif progress < 0.7:
            return 0.1
        else:
            return 0.3

# ---------------------------
# ImagePainter (批量调度)
# ---------------------------
class ImagePainter:
    def __init__(self, image_path, offset_x=0, offset_y=0):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.account_manager = AccountManager(USER_CREDENTIALS)
        self.clients = [PaintBoardClient(uid, key, "writeonly", self.account_manager)
                        for uid, key in USER_CREDENTIALS[:-1]]
        self.read_client = PaintBoardClient(USER_CREDENTIALS[-1][0], USER_CREDENTIALS[-1][1], "readonly")
        self.scheduler = None
        self.running = True

    def load_image(self):
        img = Image.open(self.image_path).convert("RGB")
        self.image_data = np.array(img)
        return True

    async def get_board_state(self):
        response = requests.get(f"{API_BASE_URL}/api/paintboard/getboard")
        board = np.frombuffer(response.content, dtype=np.uint8).reshape((600, 1000, 3))
        return board

        async def send_task(self):
        """批量调度任务：一次性分配多个点给不同账号"""
        while self.running:
            try:
                account = self.account_manager.get_available_account()
                if not account:
                    await asyncio.sleep(0.2)
                    continue

                # 找到对应客户端
                client = next((c for c in self.clients if c.uid == account['uid']), None)
                if not client or not await client.ensure_connected():
                    self.account_manager.release_account(account['uid'], account['access_key'], account.get('token'))
                    await asyncio.sleep(0.5)
                    continue

                # 批量获取工作点
                batch = self.scheduler.get_next_batch(batch_size=3)
                if not batch:
                    self.account_manager.release_account(account['uid'], account['access_key'], account.get('token'))
                    await asyncio.sleep(0.5)
                    continue

                # 逐点发送
                for (x, y, r, g, b) in batch:
                    await client.send_paint_data(x, y, r, g, b)

                # 用完账号 → 放回优先队列
                self.account_manager.release_account(account['uid'], account['access_key'], account.get('token'))

                # 随机抖动，避免集中发包
                await asyncio.sleep(random.uniform(0.1, 0.5))

            except Exception as e:
                logger.error(f"发送任务异常: {e}")
                await asyncio.sleep(1)

    async def check_and_repair(self):
        """动态采样修复检查"""
        while self.running:
            try:
                board = await self.get_board_state()
                if board is None:
                    await asyncio.sleep(5)
                    continue

                sample_rate = self.scheduler.dynamic_sample_rate()
                total_pixels = self.scheduler.total
                sample_size = int(total_pixels * sample_rate)
                sample_indices = random.sample(range(total_pixels), min(sample_size, 1000))

                repair_count = 0
                for idx in sample_indices:
                    y = idx // self.scheduler.width
                    x = idx % self.scheduler.width
                    bx, by = x + self.offset_x, y + self.offset_y
                    if bx >= 1000 or by >= 600:
                        continue
                    expected = self.scheduler.image_data[y, x]
                    actual = board[by, bx]
                    if not np.array_equal(expected, actual):
                        self.scheduler.work_queue.append((x, y))
                        repair_count += 1

                if repair_count > 0:
                    logger.info(f"修复检测发现 {repair_count} 个像素需要恢复")

                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"修复检测异常: {e}")
                await asyncio.sleep(5)

    async def run(self):
        """运行主流程"""
        if not self.load_image():
            return False

        board = await self.get_board_state()
        if board is None:
            logger.error("无法获取画板状态")
            return False

        self.scheduler = WorkScheduler(self.image_data, board, self.offset_x, self.offset_y)

        # 启动任务
        tasks = []
        for client in self.clients:
            tasks.append(asyncio.create_task(client.send_heartbeat()))
        tasks.append(asyncio.create_task(self.send_task()))
        tasks.append(asyncio.create_task(self.check_and_repair()))

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
    print("                    GenGen Painter (冷却优化版)")
    print("=" * 60)
    print("新限制优化:")
    print("  • 每个账户每30秒只能绘制1个点")
    print("  • 动态适应账户数量 (当前可用账户: {}个)".format(len(USER_CREDENTIALS)))
    print("  • 扫描线模式: 高效顺序绘制")
    print("  • 随机撒点模式: 快速覆盖")
    print("  • 智能修复: 自动检测并修复被覆盖像素")
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
        
        print("\n请选择工作模式:")
        print("1. 扫描线模式 (高效顺序绘制，推荐)")
        print("2. 随机撒点模式 (快速覆盖)")
        mode_choice = input("请输入选择 (1 或 2, 默认1): ").strip()
        
        if mode_choice == "2":
            mode = "random"
            print("已选择: 随机撒点模式")
        else:
            mode = "scanline"
            print("已选择: 扫描线模式")
            
    except ValueError:
        offset_x, offset_y = 0, 0
        mode = "scanline"
        
    return image_path, offset_x, offset_y, mode

async def main():
    image_path, offset_x, offset_y, mode = get_user_input()
    painter = ImagePainter(image_path, offset_x, offset_y, mode)
    print("\n开始绘制...")
    print(f"程序将使用 {len(USER_CREDENTIALS)-1} 个账户轮流绘制 (每账户30秒1点)")
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
        mode = "scanline"
        if len(sys.argv) > 4 and sys.argv[4] == "1":
            mode = "random"
        painter = ImagePainter(image_path, offset_x, offset_y, mode)
        asyncio.run(painter.run())
    else:
        asyncio.run(main())
