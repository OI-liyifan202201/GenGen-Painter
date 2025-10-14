import asyncio
import aiohttp
from PIL import Image
from typing import List, Dict, Optional
import logging

logger = logging.getLogger("painter")

API_URL = "https://paintboard.luogu.me"
WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

class PaintWorker:
    def __init__(self, uid: int, access_key: str, thread_id: int):
        self.uid = uid
        self.access_key = access_key
        self.thread_id = thread_id
        self.token: Optional[str] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self.queue = asyncio.Queue()
        self.paint_id = 0

    async def get_token(self):
        async with self.session.post(
            f"{API_URL}/api/auth/gettoken",
            json={"uid": self.uid, "access_key": self.access_key}
        ) as resp:
            data = await resp.json()
            if data.get("data", {}).get("token"):
                self.token = data["data"]["token"]
                logger.info(f"线程 {self.thread_id}: Token 获取成功")
                return True
            else:
                logger.error(f"线程 {self.thread_id}: Token 失败 - {data}")
                return False

    async def connect_paintboard_ws(self):
        self.ws = await self.session.ws_connect(WS_URL)
        logger.info(f"线程 {self.thread_id}: 连接 paintboard 成功")

    async def send_paint(self, x: int, y: int, r: int, g: int, b: int):
        if not self.token or not self.ws:
            return False
        
        # 构造包
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
        return True

    async def run(self, session: aiohttp.ClientSession):
        self.session = session
        if not await self.get_token():
            return
        
        await self.connect_paintboard_ws()
        self.running = True
        
        while self.running:
            try:
                pixel = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                await self.send_paint(pixel['x'], pixel['y'], pixel['r'], pixel['g'], pixel['b'])
                await asyncio.sleep(0.01)  # 10ms 粘包间隔
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"线程 {self.thread_id} 错误: {e}")

    def stop(self):
        self.running = False

class PaintManager:
    def __init__(self):
        self.workers: List[PaintWorker] = []
        self.session: Optional[aiohttp.ClientSession] = None
        self.target_image: Optional[Image.Image] = None
        self.offset_x = 0
        self.offset_y = 0
        self.board_data: Optional[bytes] = None
        self.running = False

    async def init_workers(self, accounts: List[Dict]):
        self.session = aiohttp.ClientSession()
        self.workers = [
            PaintWorker(acc['uid'], acc['key'], i)
            for i, acc in enumerate(accounts[:7])
        ]

    async def fetch_board(self):
        async with self.session.get(f"{API_URL}/api/paintboard/getboard") as resp:
            self.board_data = await resp.read()

    async def build_queues(self):
        if not self.target_image or not self.board_data:
            return

        img_w, img_h = self.target_image.size
        target_pixels = list(self.target_image.getdata())
        board = self.board_data

        # 清空队列
        for w in self.workers:
            while not w.queue.empty():
                try:
                    w.queue.get_nowait()
                except:
                    break

        block_h = 600 // len(self.workers)
        total = 0

        for y in range(600):
            worker_idx = min(y // block_h, len(self.workers) - 1)
            for x in range(1000):
                tx = x - self.offset_x
                ty = y - self.offset_y
                if 0 <= tx < img_w and 0 <= ty < img_h:
                    r, g, b = target_pixels[ty * img_w + tx]
                    board_idx = (y * 1000 + x) * 3
                    br, bg, bb = board[board_idx], board[board_idx+1], board[board_idx+2]
                    if (br, bg, bb) != (r, g, b):
                        await self.workers[worker_idx].queue.put({
                            'x': x, 'y': y, 'r': r, 'g': g, 'b': b
                        })
                        total += 1
        logger.info(f"共需绘制 {total} 像素")
        return total

    async def start(self):
        if not self.workers:
            raise ValueError("未初始化 workers")
        self.running = True
        await self.fetch_board()
        await self.build_queues()
        # 启动所有 worker
        await asyncio.gather(*[w.run(self.session) for w in self.workers])

    def stop(self):
        self.running = False
        for w in self.workers:
            w.stop()

    def set_image(self, image_path: str):
        self.target_image = Image.open(image_path).convert("RGB")

    def set_offset(self, x: int, y: int):
        self.offset_x = x
        self.offset_y = y

    def get_preview_data(self) -> dict:
        """返回预览所需数据（供前端渲染）"""
        if not self.target_image:
            return {"image": None, "offset": (0, 0)}
        
        # 将图像转为 base64（简化：只传尺寸和偏移，前端用原始文件）
        return {
            "width": self.target_image.width,
            "height": self.target_image.height,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y
        }
