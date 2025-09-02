# VibeVoice 多GPU调度系统使用说明

## 功能概述

这个增强版的Gradio演示脚本为VibeVoice添加了多GPU调度功能，可以：

- 自动检测和使用多个GPU
- 智能负载均衡，基于队列长度、内存使用率和GPU利用率选择最佳GPU
- 实时GPU状态监控
- GPU故障检测和自动恢复
- 支持指定使用特定的GPU

## 主要特性

### 🖥️ 多GPU管理
- **自动GPU检测**: 系统启动时自动检测所有可用的CUDA GPU
- **智能调度**: 根据当前负载自动选择最优GPU进行推理
- **负载均衡**: 综合考虑队列长度、内存使用率、GPU利用率进行调度

### 📊 实时监控
- **GPU状态显示**: 界面实时显示每个GPU的状态信息
- **内存监控**: 显示每个GPU的内存使用情况和可用空间
- **队列监控**: 显示每个GPU当前的任务队列长度
- **利用率监控**: 显示GPU计算利用率

### 🔧 错误处理
- **故障检测**: 自动检测GPU故障或内存不足
- **自动恢复**: 故障GPU会在后台尝试自动恢复
- **优雅降级**: GPU故障时自动切换到其他可用GPU

## 使用方法

### 基本使用

使用所有可用GPU：
```bash
python gradio_demo.py --model_path /path/to/model
```

### 指定GPU

使用特定的GPU（例如GPU 0和2）：
```bash
python gradio_demo.py --model_path /path/to/model --gpus "0,2"
```

使用单个GPU：
```bash
python gradio_demo.py --model_path /path/to/model --gpus "1"
```

### 完整参数示例

```bash
python gradio_demo.py \
    --model_path /tmp/vibevoice-model \
    --gpus "0,1,3" \
    --inference_steps 10 \
    --port 7860 \
    --share
```

## 命令行参数

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| `--model_path` | VibeVoice模型路径 | `/tmp/vibevoice-model` | `--model_path /path/to/model` |
| `--gpus` | 指定使用的GPU ID | `None` (使用所有GPU) | `--gpus "0,1,2"` |
| `--inference_steps` | 推理步数 | `10` | `--inference_steps 5` |
| `--port` | 服务器端口 | `7860` | `--port 8080` |
| `--share` | 公开分享 | `False` | `--share` |
| `--device` | *(已弃用)* | - | 多GPU模式下自动管理 |

## GPU状态界面

界面左侧的"GPU状态"部分显示：

```
🖥️ GPU状态监控

✅ GPU 0: NVIDIA GeForce RTX 4090
   📊 队列: 2 | 内存: [████████░░░░░░░░░░░░] 40.5% (16.2/40.0GB)
   ⚡ 利用率: [██████████░░░░░░░░░░] 50.2%

✅ GPU 1: NVIDIA GeForce RTX 4090  
   📊 队列: 0 | 内存: [██░░░░░░░░░░░░░░░░░░] 10.1% (4.0/40.0GB)
   ⚡ 利用率: [░░░░░░░░░░░░░░░░░░░░] 0.0%
```

## 调度算法

系统使用加权评分算法选择最佳GPU：

```
总分 = 队列长度 × 10 + 内存使用率 × 0.5 + GPU利用率 × 0.3
```

选择总分最低的GPU进行推理。

## 性能优化建议

### 并发设置
- 队列大小增加到50（从20）
- 并发限制设置为GPU数量
- 支持多个用户同时使用不同GPU

### 内存管理
- 自动GPU内存清理
- 内存不足时的智能降级
- 定期内存状态检查

### 故障恢复
- GPU故障后30秒自动尝试恢复
- 支持热插拔GPU
- 优雅的错误提示和建议

## 故障排除

### 常见问题

**Q: 某个GPU显示不可用**
A: 检查GPU是否被其他进程占用，或重启演示程序

**Q: 内存不足错误**
A: 减少并发请求数量，或使用更少的GPU

**Q: CUDA错误**
A: 检查CUDA驱动和PyTorch版本兼容性

### 日志信息

系统会输出详细的日志信息：
```
检测到 4 个GPU
将使用GPU: [0, 1, 2, 3]
初始化GPU 0: NVIDIA GeForce RTX 4090 (40.0GB)
✅ GPU 0 初始化成功
🎯 选择GPU 1进行推理 (队列长度: 0)
```

## 系统要求

- Python 3.8+
- PyTorch with CUDA support
- 多个CUDA兼容的GPU
- 足够的GPU内存（推荐每个GPU至少8GB）
- nvidia-smi工具（用于GPU利用率监控）

## 技术架构

### 核心组件

1. **GPUManager**: 多GPU管理器
2. **GPUStatus**: GPU状态数据结构
3. **智能调度器**: 负载均衡算法
4. **状态监控器**: 后台监控线程
5. **错误处理器**: 故障检测和恢复

### 线程安全
- 使用线程锁保护GPU队列操作
- 后台监控线程安全更新状态
- 支持并发推理请求

## 更新日志

- ✅ 多GPU自动检测和初始化
- ✅ 智能负载均衡调度
- ✅ 实时GPU状态监控界面
- ✅ GPU故障检测和自动恢复
- ✅ 支持指定GPU使用
- ✅ 增强错误处理和用户提示
- ✅ 性能优化和并发支持

---

如有问题或建议，请查看日志输出或联系技术支持。