from fastapi import FastAPI, WebSocket, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
import json
import os
from painter import PaintManager

app = FastAPI()

# 允许前端跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载前端静态文件
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

paint_manager = PaintManager()
PRESET_ACCOUNTS = [
    {"uid": 661094,  "key": "lDrv8W9u"},
    {"uid": 661913,  "key": "lFT03zMS"},
    {"uid": 1351126, "key": "UJUVuzyk"},
    {"uid": 1032267, "key": "6XF2wDhG"},
    {"uid": 1404345, "key": "dJvxSGv6"},
    {"uid": 1036010, "key": "hcB8wQzm"},
    {"uid": 703022,  "key": "gJNV9lrN"},
    # ... 可扩展
]

@app.on_event("startup")
async def startup():
    await paint_manager.init_workers(PRESET_ACCOUNTS)

@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file.filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    paint_manager.set_image(file_path)
    return {"filename": file.filename}

@app.post("/set_offset")
async def set_offset(x: int = Form(...), y: int = Form(...)):
    paint_manager.set_offset(x, y)
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # 发送初始预览数据
    preview = paint_manager.get_preview_data()
    await websocket.send_text(json.dumps({"type": "preview", "data": preview}))
    
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            
            if msg["type"] == "start":
                asyncio.create_task(paint_manager.start())
                await websocket.send_text(json.dumps({"type": "status", "msg": "开始绘制"}))
                
            elif msg["type"] == "stop":
                paint_manager.stop()
                await websocket.send_text(json.dumps({"type": "status", "msg": "已停止"}))
                
    except Exception as e:
        print(f"WebSocket 错误: {e}")
    finally:
        await websocket.close()
