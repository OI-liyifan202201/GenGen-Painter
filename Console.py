import asyncio
import websockets
import websockets.protocol 
import json
import threading
import time
import struct
import requests
import logging
from PIL import Image
import numpy as np
from collections import deque
from typing import List, Tuple, Optional, Dict
import sys
import os
import random

# 修复：删除末尾空格！
API_BASE_URL = "https://paintboard.luogu.me"
WEBSOCKET_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

# UID和Access Key对应表
USER_CREDENTIALS = [
    (661094, "lDrv8W9u"), (661913, "lFT03zMS"), (1351126, "UJUVuzyk"), 
    (1032267, "6XF2wDhG"), (1404345, "dJvxSGv6"), (1036010, "hcB8wQzm"), 
    (703022, "gJNV9lrN"), (1406692, "0WMtD3G7"), (1058607, "iyuq7QA2"), 
    (1276209, "vzciwZs7"), (1227240, "WwnnjHVP"), (1406674, "NtqPbU8t")
]

class PaintBoardClient:
    def __init__(self, uid: int, access_key: str, connection_type: str = "readwrite"):
        self.uid = uid
        self.access_key = access_key
        self.connection_type = connection_type
        self.token: Optional[str] = None
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None # type: ignore
        self.connected = False
        self.message_queue = deque()
        self.paint_id_counter = 0
        self.last_heartbeat = time.time()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 100
        
    async def get_token(self):
        """获取Token"""
        try:
            logger.info(f"用户 {self.uid} 正在获取 Token...")
            url = f"{API_BASE_URL}/api/auth/gettoken"
            payload = {"uid": self.uid, "access_key": self.access_key}
            response = requests.post(url, json=payload)
            result = response.json()
            
            if "data" in result:
                self.token = result["data"]["token"]
                logger.info(f"用户 {self.uid} 获取 Token 成功")
                return True
            else:
                logger.error(f"用户 {self.uid} 获取 Token 失败: {result.get('errorType', 'Unknown error')}")
                return False
        except Exception as e:
            logger.error(f"用户 {self.uid} 获取 Token 异常: {e}")
            return False

    async def connect_websocket(self):
        """连接WebSocket"""
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
            self.reconnect_attempts = 0
            logger.info(f"用户 {self.uid} 连接 WebSocket 成功 ({self.connection_type})")
            return True
        except Exception as e:
            logger.error(f"用户 {self.uid} 连接 WebSocket 失败: {e}")
            self.connected = False
            return False

    async def ensure_connected(self):
        """确保连接正常，必要时重连"""
        if not self.connected or self.websocket is None:
            if self.reconnect_attempts < self.max_reconnect_attempts:
                self.reconnect_attempts += 1
                logger.info(f"正在尝试重新连接 ({self.reconnect_attempts}/{self.max_reconnect_attempts}) 为用户 {self.uid}")
                return await self.connect_websocket()
            else:
                logger.error(f"用户 {self.uid} 达到最大重连次数")
                return False

        if self.websocket.state == websockets.protocol.State.CLOSED:
            self.connected = False
            return await self.ensure_connected()

        return True

    async def send_heartbeat(self):
        """发送心跳包"""
        if await self.ensure_connected() and self.websocket:
            try:
                await self.websocket.send(bytes([0xfb]))
                self.last_heartbeat = time.time()
            except Exception as e:
                logger.error(f"用户 {self.uid} 发送心跳失败: {e}")
                self.connected = False

    async def send_paint_data(self, x: int, y: int, r: int, g: int, b: int):
        """发送绘画数据（单个像素）"""
        if not await self.ensure_connected() or self.token is None:
            return None
            
        try:
            paint_id = self.paint_id_counter
            self.paint_id_counter = (self.paint_id_counter + 1) % 4294967296
            
            packet = bytearray()
            packet.append(0xfe)
            packet.extend(struct.pack('<H', x))
            packet.extend(struct.pack('<H', y))
            packet.append(r)
            packet.append(g)
            packet.append(b)
            packet.extend(struct.pack('<I', self.uid)[:3])
            
            token_clean = self.token.replace('-', '')
            token_bytes = bytes.fromhex(token_clean)
            packet.extend(token_bytes)
            packet.extend(struct.pack('<I', paint_id))
            
            self.message_queue.append(packet)
            return paint_id
        except Exception as e:
            logger.error(f"用户 {self.uid} 准备绘图数据失败: {e}")
            return None

    async def flush_messages(self, max_packet_size: int = 32000):
        """分块发送消息，确保每个包不超过 max_packet_size 字节（≈32KB）"""
        if not await self.ensure_connected() or len(self.message_queue) == 0 or self.websocket is None:
            return

        try:
            current_batch = []
            current_size = 0

            while self.message_queue:
                msg = self.message_queue[0]
                if current_size + len(msg) > max_packet_size:
                    if current_batch:
                        total_len = sum(len(m) for m in current_batch)
                        merged = bytearray(total_len)
                        off = 0
                        for m in current_batch:
                            merged[off:off+len(m)] = m
                            off += len(m)
                        await self.websocket.send(merged)
                        current_batch = []
                        current_size = 0
                    else:
                        # 单个消息超限？理论上不会（29B << 32KB）
                        msg = self.message_queue.popleft()
                        await self.websocket.send(msg)
                        continue

                current_batch.append(self.message_queue.popleft())
                current_size += len(msg)

            if current_batch:
                total_len = sum(len(m) for m in current_batch)
                merged = bytearray(total_len)
                off = 0
                for m in current_batch:
                    merged[off:off+len(m)] = m
                    off += len(m)
                await self.websocket.send(merged)

        except Exception as e:
            logger.error(f"用户 {self.uid} 分块发送消息失败: {e}")
            self.connected = False

    async def listen_messages(self):
        """监听服务器消息"""
        if not await self.ensure_connected() or self.websocket is None:
            return
            
        try:
            message = await asyncio.wait_for(self.websocket.recv(), timeout=0.1)
            if isinstance(message, bytes):
                self.process_binary_message(message)
        except asyncio.TimeoutError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"用户 {self.uid} 的 WebSocket 连接已关闭")
            self.connected = False
        except Exception as e:
            logger.error(f"用户 {self.uid} 接收消息异常: {e}")

    def process_binary_message(self, data: bytes):
        """处理二进制消息"""
        offset = 0
        while offset < len(data):
            if offset >= len(data):
                break
            opcode = data[offset]
            offset += 1
            
            if opcode == 0xfa:  # 绘画消息
                if offset + 7 <= len(data):
                    x = struct.unpack('<H', data[offset:offset+2])[0]
                    y = struct.unpack('<H', data[offset+2:offset+4])[0]
                    r, g, b = data[offset+4], data[offset+5], data[offset+6]
                    offset += 7
                    logger.debug(f"收到绘图消息 ({x}, {y}): RGB({r}, {g}, {b})")
                    
            elif opcode == 0xfc:  # 心跳请求 (Ping)
                asyncio.create_task(self.send_heartbeat())
                
            elif opcode == 0xff:  # 绘画结果
                if offset + 5 <= len(data):
                    paint_id = struct.unpack('<I', data[offset:offset+4])[0]
                    status = data[offset+4]
                    offset += 5
                    status_messages = {
                        0xef: "Success",
                        0xee: "Cooling down",
                        0xed: "Invalid token",
                        0xec: "Bad request",
                        0xeb: "No permission",
                        0xea: "Server error"
                    }
                    status_msg = status_messages.get(status, f"Unknown status {status:02x}")
                    logger.debug(f"绘图结果 ID {paint_id}: {status_msg}")
                    
            else:
                logger.warning(f"未知操作码: {opcode:02x}")
                break


class WorkScheduler:
    def __init__(self, image_data: np.ndarray, offset_x: int = 0, offset_y: int = 0, mode: str = "normal"):
        self.image_data = image_data
        self.height, self.width, _ = image_data.shape
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.mode = mode  # "normal" (scanline) 或 "random"

        self.initial_workload = np.ones((self.height, self.width), dtype=bool)
        self.repair_workload = np.zeros((self.height, self.width), dtype=bool)

        # 扫描线游标（仅用于 normal 模式）
        self.scanline_y = 0
        self.scanline_x = 0

        # 随机模式
        if self.mode == "random":
            self.random_indices = [(x, y) for y in range(self.height) for x in range(self.width)]
            random.shuffle(self.random_indices)
            self.random_index = 0
        else:
            self.random_indices = []
            self.random_index = 0

        self.lock = threading.Lock()
        self.initial_completed = 0
        self.repair_count = 0
        self.total_initial_pixels = self.height * self.width
        self.last_work_time = time.time()

    def get_work_chunk(self, max_pixels: int = 800, repair_priority: bool = False) -> List[Tuple[int, int, int, int, int]]:
        """获取工作块，优先修复；返回像素列表"""
        with self.lock:
            coords = []
            count = 0

            # 1. 优先处理修复任务
            if repair_priority and self.repair_count > 0:
                repair_list = []
                for y in range(self.height):
                    for x in range(self.width):
                        if self.repair_workload[y, x]:
                            repair_list.append((x, y))
                random.shuffle(repair_list)
                for x, y in repair_list[:max_pixels]:
                    r, g, b = self.image_data[y, x]
                    coords.append((x + self.offset_x, y + self.offset_y, r, g, b))
                    self.repair_workload[y, x] = False
                    self.repair_count -= 1
                    count += 1
                    if count >= max_pixels:
                        return coords

            # 2. 处理初始绘制
            remaining = max_pixels - count
            if remaining <= 0:
                return coords

            if self.mode == "random":
                added = 0
                while added < remaining and self.random_index < len(self.random_indices):
                    x, y = self.random_indices[self.random_index]
                    self.random_index += 1
                    if self.initial_workload[y, x]:
                        r, g, b = self.image_data[y, x]
                        coords.append((x + self.offset_x, y + self.offset_y, r, g, b))
                        self.initial_workload[y, x] = False
                        self.initial_completed += 1
                        added += 1
                # 如果用完，重新生成未完成的
                if self.random_index >= len(self.random_indices):
                    self.random_indices = [(x, y) for y in range(self.height) for x in range(self.width)
                                          if self.initial_workload[y, x]]
                    random.shuffle(self.random_indices)
                    self.random_index = 0

            else:
                # normal 模式：真正的扫描线
                added = 0
                while added < remaining and self.scanline_y < self.height:
                    while self.scanline_x < self.width and added < remaining:
                        if self.initial_workload[self.scanline_y, self.scanline_x]:
                            x, y = self.scanline_x, self.scanline_y
                            r, g, b = self.image_data[y, x]
                            coords.append((x + self.offset_x, y + self.offset_y, r, g, b))
                            self.initial_workload[y, x] = False
                            self.initial_completed += 1
                            added += 1
                        self.scanline_x += 1
                    if self.scanline_x >= self.width:
                        self.scanline_x = 0
                        self.scanline_y += 1

            if coords:
                self.last_work_time = time.time()
            return coords

    def add_repair_work(self, x: int, y: int):
        """添加修复任务"""
        with self.lock:
            img_x = x - self.offset_x
            img_y = y - self.offset_y
            if 0 <= img_x < self.width and 0 <= img_y < self.height:
                if not self.repair_workload[img_y, img_x]:
                    self.repair_workload[img_y, img_x] = True
                    self.repair_count += 1

    def mark_initial_complete(self, x: int, y: int):
        """标记初始绘制完成（通常不需要调用，因为 get_work_chunk 已处理）"""
        pass

    def get_progress(self) -> Tuple[int, int, int]:
        """获取进度：初始完成数，总初始数，修复数"""
        with self.lock:
            return self.initial_completed, self.total_initial_pixels, self.repair_count

    def is_initial_complete(self) -> bool:
        """检查初始绘制是否完成"""
        with self.lock:
            return self.initial_completed >= self.total_initial_pixels

    def has_work(self) -> bool:
        """检查是否有任何工作"""
        with self.lock:
            return (self.initial_completed < self.total_initial_pixels) or (self.repair_count > 0)


class ImagePainter:
    def __init__(self, image_path: str, offset_x: int = 0, offset_y: int = 0, mode: str = "normal"):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.mode = mode
        self.write_clients = []
        self.readonly_client = None
        self.image_data: Optional[np.ndarray] = None
        self.scheduler: Optional[WorkScheduler] = None
        self.workers_status = [0] * 5 
        self.current_board = None
        self.running = False
        self.repair_interval = 5
        self.last_repair_check = 0
        
        # 初始化客户端
        for i in range(5):
            uid, access_key = USER_CREDENTIALS[i % len(USER_CREDENTIALS)]
            self.write_clients.append(PaintBoardClient(uid, access_key, "writeonly"))
            
        uid, access_key = USER_CREDENTIALS[5 % len(USER_CREDENTIALS)]
        self.readonly_client = PaintBoardClient(uid, access_key, "readonly")
            
    def load_image(self):
        try:
            img = Image.open(self.image_path).convert('RGB')
            img_width, img_height = img.size
            max_width = 1000 - self.offset_x
            max_height = 600 - self.offset_y
            
            if img_width > max_width or img_height > max_height:
                ratio = min(max_width / img_width, max_height / img_height)
                new_width = int(img_width * ratio)
                new_height = int(img_height * ratio)
                resample_filter = Image.LANCZOS if hasattr(Image, 'LANCZOS') else Image.ANTIALIAS # type: ignore
                img = img.resize((new_width, new_height), resample_filter)
                logger.info(f"图片已调整大小 {img_width}x{img_height} → {new_width}x{new_height}")
            else:
                logger.info(f"图片大小 {img_width}x{img_height} 保持不变")
                
            self.image_data = np.array(img)
            self.scheduler = WorkScheduler(self.image_data, self.offset_x, self.offset_y, self.mode)
            logger.info(f"加载图片成功: {img.width}x{img.height} @ ({self.offset_x}, {self.offset_y})")
            logger.info(f"工作模式: {'随机撒点模式' if self.mode == 'random' else '扫描线模式'}")
            return True
        except Exception as e:
            logger.error(f"加载图片失败 {self.image_path}: {e}")
            return False

    async def get_board_state(self):
        """获取当前画板状态"""
        try:
            response = requests.get(f"{API_BASE_URL}/api/paintboard/getboard")
            if response.status_code == 200:
                data = response.content
                expected_size = 1000 * 600 * 3
                if len(data) != expected_size:
                    logger.error(f"画板数据大小错误: {len(data)} != {expected_size}")
                    return None

                board = np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
                self.current_board = board
                return board
            else:
                logger.error(f"获取画板失败: HTTP {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"获取画板异常: {e}")
            return None

    async def check_and_repair(self):
        """检查并修复被修改的像素"""
        if self.current_board is None or self.image_data is None or self.scheduler is None:
            await self.get_board_state()
            return
            
        board = self.current_board
        height, width = self.image_data.shape[:2]
        repair_count = 0
        
        for y in range(height):
            for x in range(width):
                board_x = x + self.offset_x
                board_y = y + self.offset_y
                if board_x >= 1000 or board_y >= 600:
                    continue
                expected = self.image_data[y, x]
                actual = board[board_y, board_x]
                if not np.array_equal(expected, actual):
                    self.scheduler.add_repair_work(board_x, board_y)
                    repair_count += 1
        
        if repair_count > 0:
            logger.info(f"发现 {repair_count} 个像素需要修复")

    async def continuous_repair(self):
        """持续修复任务"""
        while self.running:
            try:
                if self.scheduler and self.scheduler.initial_completed > self.scheduler.total_initial_pixels * 0.3:
                    current_time = time.time()
                    if current_time - self.last_repair_check >= self.repair_interval:
                        await self.get_board_state()
                        await self.check_and_repair()
                        self.last_repair_check = current_time
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"持续修复任务异常: {e}")
                await asyncio.sleep(5)

    async def paint_chunk(self, client: PaintBoardClient, coords: List[Tuple[int, int, int, int, int]]):
        """绘制一个工作块，并分批发送（≤32KB）"""
        for x, y, r, g, b in coords:
            await client.send_paint_data(x, y, r, g, b)
        await client.flush_messages(max_packet_size=32000)

    async def write_worker(self, worker_id: int):
        """写入工作线程"""
        client = self.write_clients[worker_id]
        if not client.connected:
            await client.connect_websocket()
            
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(0.05)
                continue
            
            repair_priority = self.scheduler.repair_count > 10
            coords = self.scheduler.get_work_chunk(800, repair_priority)
            
            if not coords:
                if not self.scheduler.has_work():
                    await asyncio.sleep(0.5)
                continue
            
            self.workers_status[worker_id] = len(coords)
            await self.paint_chunk(client, coords)
            await client.listen_messages()
            await asyncio.sleep(0.01)  # 更短休眠，提升响应

    async def readonly_worker(self):
        """只读工作线程，用于监听画板变化"""
        if self.readonly_client is None:
            return
        client = self.readonly_client
        if not client.connected:
            await client.connect_websocket()
        
        while self.running:
            await client.listen_messages()
            await asyncio.sleep(0.05)

    async def progress_reporter(self):
        """进度报告器"""
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(2)
                continue
            
            initial_completed, total_initial, repair_count = self.scheduler.get_progress()
            initial_progress = (initial_completed / total_initial * 100) if total_initial > 0 else 0
            active_workers = sum(1 for status in self.workers_status if status > 0)
            
            phase = "初始绘制"
            if self.scheduler.is_initial_complete():
                phase = "维护修复"
            elif initial_completed > total_initial * 0.8:
                phase = "收尾绘制"
            
            mode_display = "随机撒点" if self.mode == "random" else "扫描线"
            logger.info(f"[{phase}][{mode_display}] 初始进度: {initial_completed}/{total_initial} ({initial_progress:.1f}%) - "
                       f"修复任务: {repair_count} - 活动进程: {active_workers}/5")
            
            await asyncio.sleep(2)

    async def run(self):
        """运行绘画任务"""
        if not self.load_image():
            return False
            
        logger.info("启动绘图任务：5个写入线程 + 1个只读线程 + 1个修复线程")
        logger.info(f"图像偏移: ({self.offset_x}, {self.offset_y})")
        logger.info(f"工作模式: {'随机撒点模式' if self.mode == 'random' else '扫描线模式'}")
        
        await self.get_board_state()
        self.running = True
        tasks = []
        
        try:
            for i in range(5):
                tasks.append(asyncio.create_task(self.write_worker(i)))
            tasks.append(asyncio.create_task(self.readonly_worker()))
            tasks.append(asyncio.create_task(self.continuous_repair())) 
            tasks.append(asyncio.create_task(self.progress_reporter()))
            
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭...")
        finally:
            self.running = False
            for task in tasks:
                task.cancel()
        logger.info("绘画任务结束")
        return True


def print_banner():
    print("=" * 60)
    print("                    GenGen Painter (Optimized)")
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
        print("1. 扫描线模式 (逐行绘制，最快)")
        print("2. 随机撒点模式 (防干扰)")
        mode_choice = input("请输入选择 (1 或 2, 默认1): ").strip()
        
        mode = "random" if mode_choice == "2" else "normal"
        print(f"已选择: {'随机撒点模式' if mode == 'random' else '扫描线模式'}")
            
    except ValueError:
        offset_x, offset_y = 0, 0
        mode = "normal"
        
    return image_path, offset_x, offset_y, mode

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def main():
    image_path, offset_x, offset_y, mode = get_user_input()
    painter = ImagePainter(image_path, offset_x, offset_y, mode)
    print("\n开始绘制...")
    print("程序将同时进行初始绘制和实时修复")
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
        mode = "random" if (len(sys.argv) > 4 and sys.argv[4] == "1") else "normal"
        painter = ImagePainter(image_path, offset_x, offset_y, mode)
        asyncio.run(painter.run())
    else:
        asyncio.run(main())
