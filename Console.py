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
import math

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')https://bgithub.xyz
logger = logging.getLogger(__name__)

API_BASE_URL = "https://paintboard.luogu.me"
WEBSOCKET_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

# UID和Access Key对应表
USER_CREDENTIALS = [
    (661094, "lDrv8W9u"), (661913, "lFT03zMS"), (1351126, "UJUVuzyk"), 
    (1032267, "6XF2wDhG"), (1404345, "dJvxSGv6"), (1036010, "hcB8wQzm"), 
    (703022, "gJNV9lrN"), (1406692, "0WMtD3G7"), (1058607, "iyuq7QA2"), 
    (1276209, "vzciwZs7"), (1227240, "WwnnjHVP"), (1406674, "NtqPbU8t")
]

class RateLimiter:
    """速率限制器，确保每秒不超过256包"""
    def __init__(self):
        self.packets_per_second = 256
        self.packet_times = deque()
        self.lock = threading.Lock()
        
    async def acquire(self):
        """获取发送许可"""
        while True:
            with self.lock:
                now = time.time()
                # 移除超过1秒的时间戳
                while self.packet_times and now - self.packet_times[0] > 1.0:
                    self.packet_times.popleft()
                
                # 检查是否达到限制
                if len(self.packet_times) < self.packets_per_second:
                    self.packet_times.append(now)
                    return
            
            # 等待一段时间再检查
            await asyncio.sleep(0.001)

class PaintBoardClient:
    def __init__(self, uid: int, access_key: str, connection_type: str = "readwrite", rate_limiter: Optional[RateLimiter] = None):
        self.uid = uid
        self.access_key = access_key
        self.connection_type = connection_type
        self.rate_limiter = rate_limiter
        self.token: Optional[str] = None
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None # type: ignore
        self.connected = False
        self.message_queue = deque()
        self.paint_id_counter = 0
        self.last_heartbeat = time.time()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 100
        self.packet_size_limit = 32000  # 32KB限制
        
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
                if self.rate_limiter:
                    await self.rate_limiter.acquire()
                await self.websocket.send(bytes([0xfb]))
                self.last_heartbeat = time.time()
            except Exception as e:
                logger.error(f"用户 {self.uid} 发送心跳失败: {e}")
                self.connected = False

    async def send_paint_data(self, x: int, y: int, r: int, g: int, b: int):
        """发送绘画数据"""
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

    async def flush_messages(self):
        """发送所有排队的消息（考虑32KB包大小限制）"""
        if not await self.ensure_connected() or len(self.message_queue) == 0 or self.websocket is None:
            return
            
        try:
            # 按照32KB限制分批发送
            current_batch = bytearray()
            batches = []
            
            for msg in self.message_queue:
                if len(current_batch) + len(msg) <= self.packet_size_limit:
                    current_batch.extend(msg)
                else:
                    if current_batch:
                        batches.append(current_batch)
                    current_batch = bytearray(msg)
            
            if current_batch:
                batches.append(current_batch)
            
            # 发送所有批次
            for batch in batches:
                if self.rate_limiter:
                    await self.rate_limiter.acquire()
                await self.websocket.send(bytes(batch))
            
            self.message_queue.clear()
        except Exception as e:
            logger.error(f"用户 {self.uid} 发送消息失败: {e}")
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
        self.mode = mode  # "scanline" 或 "random"
        
        # 工作负载管理
        if mode == "scanline":
            # 扫描线模式：按行处理
            self.work_queue = self._create_scanline_workload()
        else:
            # 随机模式：随机处理
            self.work_queue = self._create_random_workload()
            
        # 修复工作负载
        self.repair_queue = deque()
        
        self.lock = threading.Lock()
        self.initial_completed = 0
        self.repair_count = 0
        self.total_initial_pixels = self.height * self.width
        self.last_work_time = time.time()
        
    def _create_scanline_workload(self):
        """创建扫描线工作负载"""
        queue = deque()
        # 按行处理，每行从左到右
        for y in range(self.height):
            for x in range(self.width):
                queue.append((x, y))
        return queue
        
    def _create_random_workload(self):
        """创建随机工作负载"""
        indices = []
        for y in range(self.height):
            for x in range(self.width):
                indices.append((x, y))
        random.shuffle(indices)
        return deque(indices)
        
    def get_work_chunk(self, chunk_size: int = 500, repair_priority: bool = False) -> List[Tuple[int, int, int, int, int]]:
        """获取工作块，优化为最大包大小"""
        with self.lock:
            coords = []
            
            # 优先处理修复任务
            if repair_priority and self.repair_queue:
                repair_count = min(chunk_size, len(self.repair_queue))
                for _ in range(repair_count):
                    if self.repair_queue:
                        x, y = self.repair_queue.popleft()
                        r, g, b = self.image_data[y, x]
                        actual_x = x + self.offset_x
                        actual_y = y + self.offset_y
                        coords.append((actual_x, actual_y, r, g, b))
                        self.repair_count -= 1
            
            # 如果没有足够的修复任务，添加初始绘制任务
            remaining = chunk_size - len(coords)
            if remaining > 0 and self.work_queue:
                for _ in range(remaining):
                    if self.work_queue:
                        x, y = self.work_queue.popleft()
                        r, g, b = self.image_data[y, x]
                        actual_x = x + self.offset_x
                        actual_y = y + self.offset_y
                        coords.append((actual_x, actual_y, r, g, b))
                        self.initial_completed += 1
            
            if coords:
                self.last_work_time = time.time()
            return coords

    def add_repair_work(self, x: int, y: int):
        """添加修复任务"""
        with self.lock:
            img_x = x - self.offset_x
            img_y = y - self.offset_y
            if 0 <= img_x < self.width and 0 <= img_y < self.height:
                self.repair_queue.append((img_x, img_y))
                self.repair_count += 1

    def mark_initial_complete(self, x: int, y: int):
        """标记初始绘制完成（在扫描线模式下优化）"""
        # 在扫描线模式下，我们按顺序处理，不需要单独标记
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
            return bool(self.work_queue or self.repair_queue)

class ImagePainter:
    def __init__(self, image_path: str, offset_x: int = 0, offset_y: int = 0, mode: str = "scanline"):
        self.image_path = image_path
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.mode = mode  # "scanline" 或 "random"
        
        # 全局速率限制器
        self.rate_limiter = RateLimiter()
        
        self.write_clients = []
        self.readonly_client = None
        self.image_data: Optional[np.ndarray] = None
        self.scheduler: Optional[WorkScheduler] = None
        self.workers_status = [0] * 5 
        self.current_board = None
        self.running = False
        self.repair_interval = 3  # 修复检查间隔（秒）
        self.last_repair_check = 0
        self.batch_size = 500  # 每批处理的像素数量，优化为接近32KB限制
        
        # 初始化客户端
        for i in range(5):
            uid, access_key = USER_CREDENTIALS[i % len(USER_CREDENTIALS)]
            self.write_clients.append(PaintBoardClient(uid, access_key, "writeonly", self.rate_limiter))
            
        uid, access_key = USER_CREDENTIALS[5 % len(USER_CREDENTIALS)]
        self.readonly_client = PaintBoardClient(uid, access_key, "readonly", self.rate_limiter)
            
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
            logger.info(f"工作模式: {'扫描线模式' if self.mode == 'scanline' else '随机撒点模式'}")
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
        
        # 优化：随机采样检查，而不是全量检查
        sample_rate = 0.1  # 10%采样率
        sample_indices = random.sample(range(height * width), int(height * width * sample_rate))
        
        for idx in sample_indices:
            y = idx // width
            x = idx % width
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
            logger.info(f"采样检查发现约 {repair_count * 10} 个像素需要修复")

    async def continuous_repair(self):
        """持续修复任务"""
        while self.running:
            try:
                # 等待初始绘制基本完成后再开始修复
                if self.scheduler and self.scheduler.initial_completed > self.scheduler.total_initial_pixels * 0.3:
                    current_time = time.time()
                    if current_time - self.last_repair_check >= self.repair_interval:
                        await self.get_board_state()
                        await self.check_and_repair()
                        self.last_repair_check = current_time
                
                await asyncio.sleep(1)  # 短暂休眠避免过于频繁
            except Exception as e:
                logger.error(f"持续修复任务异常: {e}")
                await asyncio.sleep(5)

    async def paint_chunk(self, client: PaintBoardClient, coords: List[Tuple[int, int, int, int, int]]):
        """绘制一个工作块"""
        for x, y, r, g, b in coords:
            await client.send_paint_data(x, y, r, g, b)
        
        # 批量发送，利用速率限制
        await client.flush_messages()

    async def write_worker(self, worker_id: int):
        """写入工作线程"""
        client = self.write_clients[worker_id]
        if not client.connected:
            await client.connect_websocket()
            
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(0.05)
                continue
            
            # 动态调整优先级：如果有修复任务，优先处理
            repair_priority = self.scheduler.repair_count > 50
            coords = self.scheduler.get_work_chunk(self.batch_size, repair_priority)
            
            if not coords:
                # 如果没有工作，检查是否需要等待
                if not self.scheduler.has_work():
                    await asyncio.sleep(0.5)
                continue
            
            self.workers_status[worker_id] = len(coords)
            await self.paint_chunk(client, coords)
            await client.listen_messages()
            await asyncio.sleep(0.01)  # 减少休眠时间，提高效率

    async def readonly_worker(self):
        """只读工作线程，用于监听画板变化"""
        if self.readonly_client is None:
            return
        client = self.readonly_client
        if not client.connected:
            await client.connect_websocket()
        
        while self.running:
            await client.listen_messages()
            await asyncio.sleep(0.05)  # 更频繁地监听

    async def progress_reporter(self):
        """进度报告器"""
        phase = "初始绘制"
        while self.running:
            if self.scheduler is None:
                await asyncio.sleep(2)
                continue
            
            initial_completed, total_initial, repair_count = self.scheduler.get_progress()
            initial_progress = (initial_completed / total_initial * 100) if total_initial > 0 else 0
            active_workers = sum(1 for status in self.workers_status if status > 0)
            
            # 更新阶段显示
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
        logger.info(f"工作模式: {'扫描线模式' if self.mode == 'scanline' else '随机撒点模式'}")
        logger.info(f"速率限制: 256包/秒，每包最大32KB")
        
        # 获取初始画板状态
        await self.get_board_state()
        self.running = True
        tasks = []
        
        try:
            # 启动所有工作线程
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
    print("                    GenGen Painter (优化版)")
    print("=" * 60)
    print("新限制优化:")
    print("  • 每秒256包限制")
    print("  • 每包32KB大小限制")
    print("  • 扫描线模式: 高效顺序绘制")
    print("  • 随机撒点模式: 快速覆盖")
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
        mode = "scanline"
        if len(sys.argv) > 4 and sys.argv[4] == "1":
            mode = "random"
        painter = ImagePainter(image_path, offset_x, offset_y, mode)
        asyncio.run(painter.run())
    else:
        asyncio.run(main())

