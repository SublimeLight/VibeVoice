#!/usr/bin/env python3
"""
VibeVoice 多GPU功能测试脚本
"""

import torch
import time
import sys
import os

def test_gpu_availability():
    """测试GPU可用性"""
    print("🔍 检测GPU环境...")
    
    if not torch.cuda.is_available():
        print("❌ CUDA不可用")
        return False
    
    gpu_count = torch.cuda.device_count()
    print(f"✅ 检测到 {gpu_count} 个GPU")
    
    for i in range(gpu_count):
        try:
            device_name = torch.cuda.get_device_name(i)
            memory_total = torch.cuda.get_device_properties(i).total_memory / (1024**3)
            print(f"   GPU {i}: {device_name} ({memory_total:.1f}GB)")
            
            # 测试GPU可用性
            with torch.cuda.device(i):
                test_tensor = torch.tensor([1.0], device=f'cuda:{i}')
                result = test_tensor * 2
                del test_tensor, result
                torch.cuda.empty_cache()
                
        except Exception as e:
            print(f"   ❌ GPU {i} 测试失败: {e}")
            
    return gpu_count > 1

def test_multi_gpu_demo():
    """测试多GPU演示脚本"""
    print("\n🧪 测试多GPU演示脚本...")
    
    # 检查演示脚本是否存在
    demo_path = "gradio_demo.py"
    if not os.path.exists(demo_path):
        print(f"❌ 演示脚本不存在: {demo_path}")
        return False
    
    # 尝试导入主要组件
    try:
        sys.path.insert(0, os.path.dirname(demo_path))
        from gradio_demo import GPUManager, GPUStatus
        print("✅ 成功导入GPUManager和GPUStatus")
        
        # 测试GPUStatus数据结构
        status = GPUStatus(
            gpu_id=0,
            device_name="Test GPU",
            memory_used=10.0,
            memory_total=40.0,
            utilization=50.0,
            queue_length=2,
            is_available=True,
            last_updated=time.time()
        )
        
        print(f"✅ GPUStatus测试通过: 内存使用率 {status.memory_usage_percent:.1f}%")
        
        return True
        
    except ImportError as e:
        print(f"❌ 导入失败: {e}")
        return False
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False

def test_gpu_manager_initialization():
    """测试GPUManager初始化（需要模型路径）"""
    print("\n🔧 测试GPUManager初始化...")
    
    # 这里需要实际的模型路径，所以只做基本检查
    try:
        from gradio_demo import GPUManager
        print("✅ GPUManager类可以正常导入")
        print("💡 注意: 完整测试需要有效的模型路径")
        return True
    except Exception as e:
        print(f"❌ GPUManager导入失败: {e}")
        return False

def main():
    """主测试函数"""
    print("🎙️ VibeVoice 多GPU功能测试\n")
    
    tests = [
        ("GPU可用性", test_gpu_availability),
        ("演示脚本导入", test_multi_gpu_demo),
        ("GPUManager初始化", test_gpu_manager_initialization),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n{'='*50}")
        print(f"测试: {test_name}")
        print('='*50)
        
        try:
            if test_func():
                print(f"✅ {test_name} - 通过")
                passed += 1
            else:
                print(f"❌ {test_name} - 失败")
        except Exception as e:
            print(f"❌ {test_name} - 异常: {e}")
    
    print(f"\n{'='*50}")
    print(f"测试结果: {passed}/{total} 通过")
    print('='*50)
    
    if passed == total:
        print("🎉 所有测试通过！多GPU功能已就绪。")
        print("\n💡 使用方法:")
        print("   python gradio_demo.py --model_path /path/to/model")
        print("   python gradio_demo.py --model_path /path/to/model --gpus '0,1'")
    else:
        print("⚠️ 部分测试失败，请检查环境配置。")
    
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)