# 🎨 LGS Paintboard 2026 自动绘画工具

> **Python 后端 + Web 前端** 实现的高性能、多线程、实时预览自动绘画工具，专为 [洛谷 LGS Paintboard 2026](https://paintboard.luogu.me) 设计。

![Paintboard Preview](https://via.placeholder.com/800x400/e0e0e0/000000?text=Real-time+Preview+Demo)  
*（实际界面包含画板预览、进度条、控制面板）*

---

## ✨ 核心特性

- ✅ **7 线程并发**：充分利用 IP 最大 7 连接限制，极速绘制
- ✅ **原像素绘制**：不缩放图片，按原始尺寸+偏移精准绘制
- ✅ **实时预览**：1000×600 画板可视化，支持拖拽调整位置
- ✅ **智能防覆盖**：实时监听他人绘制，被覆盖像素自动重绘
- ✅ **冷却重试**：收到冷却响应 (`0xee`) 自动延迟重试
- ✅ **断线重连**：WebSocket 断开后自动退避重连
- ✅ **动态负载均衡**：空闲线程自动从其他队列偷取任务
- ✅ **粘包优化**：每 10ms 批量发送，避免触发限流

---

## 📦 项目结构

```bash
paintboard-auto/
├── backend/               # Python 后端
│   ├── __init__.py        # 标记为 Python 包
│   ├── main.py            # FastAPI 入口
│   └── painter.py         # 绘画核心逻辑
├── frontend/              # Web 前端
│   └── index.html         # 控制界面 + 实时预览
├── uploads/               # 上传图片临时存储（自动创建）
├── requirements.txt       # Python 依赖
└── README.md
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
# 克隆项目（或下载 ZIP 解压）
git clone https://github.com/yourname/paintboard-auto.git
cd paintboard-auto

# 创建虚拟环境（推荐）
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置账户（可选）

默认已内置你提供的 12 组账号（取前 7 个）：
```python
# backend/painter.py 中的 PRESET_ACCOUNTS
[
  {"uid": 661094,  "key": "lDrv8W9u"},
  {"uid": 661913,  "key": "lFT03zMS"},
  # ... 共 12 组
]
```

> 💡 如需自定义账号，修改 `backend/painter.py` 中的 `PRESET_ACCOUNTS` 列表。

### 3. 启动服务

```bash
# 确保在项目根目录（有 backend/ 和 frontend/ 的目录）
uvicorn backend.main:app --reload --port 8000
```

### 4. 访问前端

打开浏览器访问：  
👉 **http://localhost:8000/static/index.html**

---

## 🖼️ 使用指南

1. **上传图片**  
   - 点击 "选择文件" 上传本地图片（支持 PNG/JPG）
   - 或输入图片 URL（需 CORS 允许）

2. **调整位置**  
   - 修改 "偏移 X/Y" 控制图片在画板上的位置
   - 预览面板实时更新（**原像素显示，不缩放**）

3. **开始绘制**  
   - 点击 "▶ 开始" 按钮
   - 7 个线程自动分配任务，进度条实时更新

4. **监控状态**  
   - 日志面板显示连接/绘制状态
   - 被覆盖像素自动重绘（无需人工干预）

---

## ⚙️ 技术细节

### 后端架构
| 组件 | 技术 |
|------|------|
| Web 框架 | FastAPI |
| 异步 HTTP | aiohttp |
| 图像处理 | Pillow (PIL) |
| WebSocket | websockets + aiohttp |
| 并发模型 | asyncio (异步非阻塞) |

### 前端架构
| 组件 | 技术 |
|------|------|
| UI 框架 | 原生 HTML/CSS/JS |
| 画板渲染 | Canvas API |
| 通信协议 | WebSocket |
| 响应式 | Flexbox 布局 |

### 关键优化
- **粘包发送**：每 10ms 合并多个绘制请求，避免触发 128 包/秒限制
- **动态分块**：画板按 Y 轴均分 7 块，每线程负责一块
- **覆盖检测**：监听 `0xfa` 消息，实时比对目标图差异
- **冷却处理**：收到 `0xee` 状态码后 2 秒重试

---

## 🛠️ 故障排除

### 常见问题

| 问题 | 解决方案 |
|------|----------|
| `ModuleNotFoundError: No module named 'painter'` | 确保 `backend/__init__.py` 存在，并在项目根目录运行 uvicorn |
| `Directory '../frontend' does not exist` | 检查 `frontend/index.html` 是否存在，路径是否正确 |
| `NameError: name 'app' is not defined` | 确保 `app.mount()` 在 `app = FastAPI()` **之后** |
| 连接被拒绝 (429) | 降低绘制频率（修改 `painter.py` 中的 `await asyncio.sleep(0.01)`） |
| Token 无效 | 检查 UID 和 AccessKey 是否正确，账户是否被封禁 |

### 调试技巧
1. 查看后端日志中的路径打印：
   ```log
   前端目录: C:\path\to\frontend (存在: True)
   ```
2. 在浏览器开发者工具中检查 WebSocket 连接状态
3. 临时注释 `app.mount()` 测试纯后端功能

---

## 📜 许可证

本项目仅供学习交流，请遵守 [洛谷 Paintboard 使用条款](https://paintboard.luogu.me)。  
禁止用于恶意刷屏、破坏他人作品等行为。

---

## 💬 支持与贡献

- **问题反馈**：提交 Issue
- **功能建议**：PR 欢迎！
- **特别鸣谢**：洛谷社区 & Paintboard 开发者

> **Happy Painting!** 🎨  
> *—— 用代码在冬日画板上留下你的印记* ❄️
