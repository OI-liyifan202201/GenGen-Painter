# 🎨 LGS Paintboard 2026 自动绘画工具

> **Python 后端 + Web 前端** 实现的高性能、多线程、实时预览自动绘画工具，专为 [洛谷 LGS Paintboard 2026](https://paintboard.luogu.me) 设计。

`pip install PyQt6 PyQt6-Fluent-Widgets pillow numpy websockets requests`
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
