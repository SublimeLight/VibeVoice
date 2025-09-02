"""
VibeVoice Gradio Demo - High-Quality Dialogue Generation Interface with Streaming Support
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Any, Iterator
from datetime import datetime
import threading
import queue
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import gradio as gr
import librosa
import soundfile as sf
import torch
import os
import traceback
import psutil
import subprocess

from vibevoice.modular.configuration_vibevoice import VibeVoiceConfig
from vibevoice.modular.modeling_vibevoice_inference import VibeVoiceForConditionalGenerationInference
from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor
from vibevoice.modular.streamer import AudioStreamer
from transformers.utils import logging
from transformers import set_seed

logging.set_verbosity_info()
logger = logging.get_logger(__name__)


@dataclass
class GPUStatus:
    """GPU状态信息"""
    gpu_id: int
    device_name: str
    memory_used: float  # GB
    memory_total: float  # GB
    utilization: float  # 百分比
    queue_length: int
    is_available: bool
    last_updated: float  # 时间戳
    
    @property
    def memory_free(self) -> float:
        return self.memory_total - self.memory_used
    
    @property
    def memory_usage_percent(self) -> float:
        return (self.memory_used / self.memory_total) * 100 if self.memory_total > 0 else 0


class GPUManager:
    """多GPU管理器，负责GPU调度和负载均衡"""
    
    def __init__(self, model_path: str, inference_steps: int = 5, gpu_ids: List[int] = None):
        self.model_path = model_path
        self.inference_steps = inference_steps
        self.target_gpu_ids = gpu_ids  # 指定要使用的GPU ID列表
        self.gpu_instances = {}  # gpu_id -> VibeVoiceDemo实例
        self.gpu_status = {}     # gpu_id -> GPUStatus
        self.gpu_queues = {}     # gpu_id -> Queue
        self.gpu_locks = {}      # gpu_id -> threading.Lock
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.status_update_thread = None
        self.stop_monitoring = False
        
        # 初始化可用GPU
        self._initialize_gpus()
        self._start_monitoring()
    
    def _initialize_gpus(self):
        """初始化指定的GPU"""
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA不可用，无法使用GPU")
        
        gpu_count = torch.cuda.device_count()
        print(f"检测到 {gpu_count} 个GPU")
        
        # 确定要使用的GPU ID列表
        if self.target_gpu_ids is None:
            # 如果未指定，使用所有可用GPU
            target_gpus = list(range(gpu_count))
        else:
            # 验证指定的GPU ID是否有效
            target_gpus = []
            for gpu_id in self.target_gpu_ids:
                if 0 <= gpu_id < gpu_count:
                    target_gpus.append(gpu_id)
                else:
                    print(f"⚠️ 警告: GPU {gpu_id} 不存在，已跳过")
            
            if not target_gpus:
                raise RuntimeError("没有有效的GPU ID被指定")
        
        print(f"将使用GPU: {target_gpus}")
        
        for gpu_id in target_gpus:
            try:
                # 获取GPU信息
                device_name = torch.cuda.get_device_name(gpu_id)
                memory_total = torch.cuda.get_device_properties(gpu_id).total_memory / (1024**3)  # GB
                
                print(f"初始化GPU {gpu_id}: {device_name} ({memory_total:.1f}GB)")
                
                # 创建VibeVoiceDemo实例
                demo_instance = VibeVoiceDemo(
                    model_path=self.model_path,
                    device=f"cuda:{gpu_id}",
                    inference_steps=self.inference_steps
                )
                
                # 存储实例和状态
                self.gpu_instances[gpu_id] = demo_instance
                self.gpu_queues[gpu_id] = queue.Queue()
                self.gpu_locks[gpu_id] = threading.Lock()
                
                # 初始化GPU状态
                self.gpu_status[gpu_id] = GPUStatus(
                    gpu_id=gpu_id,
                    device_name=device_name,
                    memory_used=0.0,
                    memory_total=memory_total,
                    utilization=0.0,
                    queue_length=0,
                    is_available=True,
                    last_updated=time.time()
                )
                
                print(f"✅ GPU {gpu_id} 初始化成功")
                
            except Exception as e:
                print(f"❌ GPU {gpu_id} 初始化失败: {e}")
                self.gpu_status[gpu_id] = GPUStatus(
                    gpu_id=gpu_id,
                    device_name=f"GPU {gpu_id} (Error)",
                    memory_used=0.0,
                    memory_total=0.0,
                    utilization=0.0,
                    queue_length=0,
                    is_available=False,
                    last_updated=time.time()
                )
    
    def _start_monitoring(self):
        """启动GPU状态监控线程"""
        self.status_update_thread = threading.Thread(target=self._monitor_gpu_status, daemon=True)
        self.status_update_thread.start()
    
    def _monitor_gpu_status(self):
        """监控GPU状态的后台线程"""
        while not self.stop_monitoring:
            try:
                for gpu_id in self.gpu_instances.keys():
                    if gpu_id in self.gpu_status:
                        # 更新GPU内存使用情况
                        with torch.cuda.device(gpu_id):
                            memory_used = torch.cuda.memory_allocated(gpu_id) / (1024**3)  # GB
                            memory_reserved = torch.cuda.memory_reserved(gpu_id) / (1024**3)  # GB
                        
                        # 更新队列长度
                        queue_length = self.gpu_queues[gpu_id].qsize()
                        
                        # 获取GPU利用率（如果nvidia-ml-py可用）
                        utilization = self._get_gpu_utilization(gpu_id)
                        
                        # 更新状态
                        self.gpu_status[gpu_id].memory_used = max(memory_used, memory_reserved)
                        self.gpu_status[gpu_id].utilization = utilization
                        self.gpu_status[gpu_id].queue_length = queue_length
                        self.gpu_status[gpu_id].last_updated = time.time()
                
                time.sleep(5)  # 每5秒更新一次
                
            except Exception as e:
                print(f"GPU状态监控错误: {e}")
                time.sleep(10)  # 发生错误时等待更长时间
    
    def _get_gpu_utilization(self, gpu_id: int) -> float:
        """获取GPU利用率"""
        try:
            # 尝试使用nvidia-smi获取GPU利用率
            result = subprocess.run([
                'nvidia-smi', '--query-gpu=utilization.gpu',
                '--format=csv,noheader,nounits', f'--id={gpu_id}'
            ], capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                return float(result.stdout.strip())
        except:
            pass
        
        return 0.0  # 如果无法获取，返回0
    
    def select_best_gpu(self) -> int:
        """选择最佳GPU进行推理"""
        available_gpus = [
            gpu_id for gpu_id, status in self.gpu_status.items()
            if status.is_available
        ]
        
        if not available_gpus:
            # 尝试重新检查GPU状态
            self._check_gpu_health()
            available_gpus = [
                gpu_id for gpu_id, status in self.gpu_status.items()
                if status.is_available
            ]
            
            if not available_gpus:
                raise RuntimeError("没有可用的GPU。请检查GPU状态或重启服务。")
        
        # 计算每个GPU的负载分数（越低越好）
        best_gpu = None
        best_score = float('inf')
        
        for gpu_id in available_gpus:
            status = self.gpu_status[gpu_id]
            
            # 综合考虑队列长度、内存使用率和GPU利用率
            queue_score = status.queue_length * 10  # 队列长度权重
            memory_score = status.memory_usage_percent * 0.5  # 内存使用率权重
            util_score = status.utilization * 0.3  # GPU利用率权重
            
            total_score = queue_score + memory_score + util_score
            
            if total_score < best_score:
                best_score = total_score
                best_gpu = gpu_id
        
        return best_gpu if best_gpu is not None else available_gpus[0]
    
    def _check_gpu_health(self):
        """检查GPU健康状态并尝试恢复"""
        for gpu_id in list(self.gpu_instances.keys()):
            try:
                # 尝试在GPU上执行简单操作来检查健康状态
                with torch.cuda.device(gpu_id):
                    test_tensor = torch.tensor([1.0], device=f'cuda:{gpu_id}')
                    test_result = test_tensor * 2
                    del test_tensor, test_result
                    torch.cuda.empty_cache()
                
                # 如果操作成功，标记为可用
                if gpu_id in self.gpu_status:
                    self.gpu_status[gpu_id].is_available = True
                    print(f"✅ GPU {gpu_id} 健康检查通过")
                    
            except Exception as e:
                # 如果操作失败，标记为不可用
                if gpu_id in self.gpu_status:
                    self.gpu_status[gpu_id].is_available = False
                    print(f"❌ GPU {gpu_id} 健康检查失败: {e}")
    
    def _handle_gpu_error(self, gpu_id: int, error: Exception):
        """处理GPU错误"""
        print(f"⚠️ GPU {gpu_id} 发生错误: {error}")
        
        # 标记GPU为不可用
        if gpu_id in self.gpu_status:
            self.gpu_status[gpu_id].is_available = False
        
        # 清理GPU内存
        try:
            with torch.cuda.device(gpu_id):
                torch.cuda.empty_cache()
        except:
            pass
        
        # 尝试重新初始化GPU（在后台线程中）
        def recover_gpu():
            time.sleep(30)  # 等待30秒后尝试恢复
            try:
                print(f"🔄 尝试恢复GPU {gpu_id}...")
                self._check_gpu_health()
                if gpu_id in self.gpu_status and self.gpu_status[gpu_id].is_available:
                    print(f"✅ GPU {gpu_id} 已恢复")
                else:
                    print(f"❌ GPU {gpu_id} 恢复失败")
            except Exception as e:
                print(f"❌ GPU {gpu_id} 恢复过程中发生错误: {e}")
        
        recovery_thread = threading.Thread(target=recover_gpu, daemon=True)
        recovery_thread.start()
    
    def get_gpu_status_summary(self) -> str:
        """获取GPU状态摘要"""
        if not self.gpu_status:
            return "无GPU状态信息"
        
        summary_lines = ["🖥️ **GPU状态监控**\n"]
        
        for gpu_id, status in sorted(self.gpu_status.items()):
            status_icon = "✅" if status.is_available else "❌"
            memory_bar = self._create_progress_bar(status.memory_usage_percent, 20)
            util_bar = self._create_progress_bar(status.utilization, 20)
            
            summary_lines.append(
                f"{status_icon} **GPU {gpu_id}**: {status.device_name}\n"
                f"   📊 队列: {status.queue_length} | "
                f"内存: {memory_bar} {status.memory_usage_percent:.1f}% "
                f"({status.memory_used:.1f}/{status.memory_total:.1f}GB)\n"
                f"   ⚡ 利用率: {util_bar} {status.utilization:.1f}%\n"
            )
        
        return "\n".join(summary_lines)
    
    def _create_progress_bar(self, percentage: float, width: int = 20) -> str:
        """创建进度条"""
        filled = int(width * percentage / 100)
        bar = "█" * filled + "░" * (width - filled)
        return f"[{bar}]"
    
    def generate_podcast_streaming(self, *args, **kwargs):
        """使用最佳GPU生成播客"""
        selected_gpu = None
        
        try:
            # 选择最佳GPU
            selected_gpu = self.select_best_gpu()
            
            print(f"🎯 选择GPU {selected_gpu}进行推理 (队列长度: {self.gpu_status[selected_gpu].queue_length})")
            
            # 将任务添加到选定GPU的队列
            with self.gpu_locks[selected_gpu]:
                self.gpu_queues[selected_gpu].put(1)  # 添加任务计数
            
            # 使用选定GPU的实例进行推理
            demo_instance = self.gpu_instances[selected_gpu]
            
            # 生成播客
            for result in demo_instance.generate_podcast_streaming(*args, **kwargs):
                yield result
                
        except Exception as e:
            # 处理GPU相关错误
            if selected_gpu is not None:
                self._handle_gpu_error(selected_gpu, e)
            
            error_msg = f"❌ GPU推理失败: {str(e)}"
            if "out of memory" in str(e).lower():
                error_msg += "\n💡 建议: GPU内存不足，请尝试减少并发请求或重启服务"
            elif "cuda" in str(e).lower():
                error_msg += "\n💡 建议: CUDA错误，GPU可能需要重启"
            
            print(error_msg)
            
            # 返回错误状态
            yield None, None, error_msg, gr.update(visible=False)
            
        finally:
            # 任务完成后从队列中移除
            if selected_gpu is not None:
                with self.gpu_locks[selected_gpu]:
                    try:
                        self.gpu_queues[selected_gpu].get_nowait()
                    except queue.Empty:
                        pass
    
    def stop_audio_generation(self):
        """停止所有GPU上的音频生成"""
        for demo_instance in self.gpu_instances.values():
            demo_instance.stop_audio_generation()
    
    def shutdown(self):
        """关闭GPU管理器"""
        self.stop_monitoring = True
        if self.status_update_thread:
            self.status_update_thread.join(timeout=5)
        self.executor.shutdown(wait=True)


class VibeVoiceDemo:
    def __init__(self, model_path: str, device: str = "cuda", inference_steps: int = 5):
        """Initialize the VibeVoice demo with model loading."""
        self.model_path = model_path
        self.device = device
        self.inference_steps = inference_steps
        self.is_generating = False  # Track generation state
        self.stop_generation = False  # Flag to stop generation
        self.current_streamer = None  # Track current audio streamer
        self.load_model()
        self.setup_voice_presets()
        self.load_example_scripts()  # Load example scripts
        
    def load_model(self):
        """Load the VibeVoice model and processor."""
        print(f"Loading processor & model from {self.model_path}")
        
        # Load processor
        self.processor = VibeVoiceProcessor.from_pretrained(
            self.model_path,
        )
        
        # Load model
        self.model = VibeVoiceForConditionalGenerationInference.from_pretrained(
            self.model_path,
            torch_dtype=torch.bfloat16,
            device_map=self.device,  # 使用指定的设备
            attn_implementation="flash_attention_2",
        )
        self.model.eval()
        
        # Use SDE solver by default
        self.model.model.noise_scheduler = self.model.model.noise_scheduler.from_config(
            self.model.model.noise_scheduler.config, 
            algorithm_type='sde-dpmsolver++',
            beta_schedule='squaredcos_cap_v2'
        )
        self.model.set_ddpm_inference_steps(num_steps=self.inference_steps)
        
        if hasattr(self.model.model, 'language_model'):
            print(f"Language model attention: {self.model.model.language_model.config._attn_implementation}")
    
    def setup_voice_presets(self):
        """Setup voice presets by scanning the voices directory."""
        voices_dir = os.path.join(os.path.dirname(__file__), "voices")
        
        # Check if voices directory exists
        if not os.path.exists(voices_dir):
            print(f"Warning: Voices directory not found at {voices_dir}")
            self.voice_presets = {}
            self.available_voices = {}
            return
        
        # Scan for all WAV files in the voices directory
        self.voice_presets = {}
        
        # Get all .wav files in the voices directory
        wav_files = [f for f in os.listdir(voices_dir) 
                    if f.lower().endswith(('.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac')) and os.path.isfile(os.path.join(voices_dir, f))]
        
        # Create dictionary with filename (without extension) as key
        for wav_file in wav_files:
            # Remove .wav extension to get the name
            name = os.path.splitext(wav_file)[0]
            # Create full path
            full_path = os.path.join(voices_dir, wav_file)
            self.voice_presets[name] = full_path
        
        # Sort the voice presets alphabetically by name for better UI
        self.voice_presets = dict(sorted(self.voice_presets.items()))
        
        # Filter out voices that don't exist (this is now redundant but kept for safety)
        self.available_voices = {
            name: path for name, path in self.voice_presets.items()
            if os.path.exists(path)
        }
        
        if not self.available_voices:
            raise gr.Error("No voice presets found. Please add .wav files to the demo/voices directory.")
        
        print(f"Found {len(self.available_voices)} voice files in {voices_dir}")
        print(f"Available voices: {', '.join(self.available_voices.keys())}")
    
    def read_audio(self, audio_path: str, target_sr: int = 24000) -> np.ndarray:
        """Read and preprocess audio file."""
        try:
            wav, sr = sf.read(audio_path)
            if len(wav.shape) > 1:
                wav = np.mean(wav, axis=1)
            if sr != target_sr:
                wav = librosa.resample(wav, orig_sr=sr, target_sr=target_sr)
            return wav
        except Exception as e:
            print(f"Error reading audio {audio_path}: {e}")
            return np.array([])
    
    def generate_podcast_streaming(self, 
                                 num_speakers: int,
                                 script: str,
                                 speaker_1: str = None,
                                 speaker_2: str = None,
                                 speaker_3: str = None,
                                 speaker_4: str = None,
                                 cfg_scale: float = 1.3) -> Iterator[tuple]:
        try:
            
            # Reset stop flag and set generating state
            self.stop_generation = False
            self.is_generating = True
            
            # Validate inputs
            if not script.strip():
                self.is_generating = False
                raise gr.Error("Error: Please provide a script.")

            # Defend against common mistake
            script = script.replace("’", "'")
            
            if num_speakers < 1 or num_speakers > 4:
                self.is_generating = False
                raise gr.Error("Error: Number of speakers must be between 1 and 4.")
            
            # Collect selected speakers
            selected_speakers = [speaker_1, speaker_2, speaker_3, speaker_4][:num_speakers]
            
            # Validate speaker selections
            for i, speaker in enumerate(selected_speakers):
                if not speaker or speaker not in self.available_voices:
                    self.is_generating = False
                    raise gr.Error(f"Error: Please select a valid speaker for Speaker {i+1}.")
            
            # Build initial log
            log = f"🎙️ Generating podcast with {num_speakers} speakers\n"
            log += f"📊 Parameters: CFG Scale={cfg_scale}, Inference Steps={self.inference_steps}\n"
            log += f"🎭 Speakers: {', '.join(selected_speakers)}\n"
            
            # Check for stop signal
            if self.stop_generation:
                self.is_generating = False
                yield None, "🛑 Generation stopped by user", gr.update(visible=False)
                return
            
            # Load voice samples
            voice_samples = []
            for speaker_name in selected_speakers:
                audio_path = self.available_voices[speaker_name]
                audio_data = self.read_audio(audio_path)
                if len(audio_data) == 0:
                    self.is_generating = False
                    raise gr.Error(f"Error: Failed to load audio for {speaker_name}")
                voice_samples.append(audio_data)
            
            # log += f"✅ Loaded {len(voice_samples)} voice samples\n"
            
            # Check for stop signal
            if self.stop_generation:
                self.is_generating = False
                yield None, "🛑 Generation stopped by user", gr.update(visible=False)
                return
            
            # Parse script to assign speaker ID's
            lines = script.strip().split('\n')
            formatted_script_lines = []
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                    
                # Check if line already has speaker format
                if line.startswith('Speaker ') and ':' in line:
                    formatted_script_lines.append(line)
                else:
                    # Auto-assign to speakers in rotation
                    speaker_id = len(formatted_script_lines) % num_speakers
                    formatted_script_lines.append(f"Speaker {speaker_id}: {line}")
            
            formatted_script = '\n'.join(formatted_script_lines)
            log += f"📝 Formatted script with {len(formatted_script_lines)} turns\n\n"
            log += "🔄 Processing with VibeVoice (streaming mode)...\n"
            
            # Check for stop signal before processing
            if self.stop_generation:
                self.is_generating = False
                yield None, "🛑 Generation stopped by user", gr.update(visible=False)
                return
            
            start_time = time.time()
            
            inputs = self.processor(
                text=[formatted_script],
                voice_samples=[voice_samples],
                padding=True,
                return_tensors="pt",
                return_attention_mask=True,
            )
            
            # Create audio streamer
            audio_streamer = AudioStreamer(
                batch_size=1,
                stop_signal=None,
                timeout=None
            )
            
            # Store current streamer for potential stopping
            self.current_streamer = audio_streamer
            
            # Start generation in a separate thread
            generation_thread = threading.Thread(
                target=self._generate_with_streamer,
                args=(inputs, cfg_scale, audio_streamer)
            )
            generation_thread.start()
            
            # Wait for generation to actually start producing audio
            time.sleep(1)  # Reduced from 3 to 1 second

            # Check for stop signal after thread start
            if self.stop_generation:
                audio_streamer.end()
                generation_thread.join(timeout=5.0)  # Wait up to 5 seconds for thread to finish
                self.is_generating = False
                yield None, "🛑 Generation stopped by user", gr.update(visible=False)
                return

            # Collect audio chunks as they arrive
            sample_rate = 24000
            all_audio_chunks = []  # For final statistics
            pending_chunks = []  # Buffer for accumulating small chunks
            chunk_count = 0
            last_yield_time = time.time()
            min_yield_interval = 15 # Yield every 15 seconds
            min_chunk_size = sample_rate * 30 # At least 2 seconds of audio
            
            # Get the stream for the first (and only) sample
            audio_stream = audio_streamer.get_stream(0)
            
            has_yielded_audio = False
            has_received_chunks = False  # Track if we received any chunks at all
            
            for audio_chunk in audio_stream:
                # Check for stop signal in the streaming loop
                if self.stop_generation:
                    audio_streamer.end()
                    break
                    
                chunk_count += 1
                has_received_chunks = True  # Mark that we received at least one chunk
                
                # Convert tensor to numpy
                if torch.is_tensor(audio_chunk):
                    # Convert bfloat16 to float32 first, then to numpy
                    if audio_chunk.dtype == torch.bfloat16:
                        audio_chunk = audio_chunk.float()
                    audio_np = audio_chunk.cpu().numpy().astype(np.float32)
                else:
                    audio_np = np.array(audio_chunk, dtype=np.float32)
                
                # Ensure audio is 1D and properly normalized
                if len(audio_np.shape) > 1:
                    audio_np = audio_np.squeeze()
                
                # Convert to 16-bit for Gradio
                audio_16bit = convert_to_16_bit_wav(audio_np)
                
                # Store for final statistics
                all_audio_chunks.append(audio_16bit)
                
                # Add to pending chunks buffer
                pending_chunks.append(audio_16bit)
                
                # Calculate pending audio size
                pending_audio_size = sum(len(chunk) for chunk in pending_chunks)
                current_time = time.time()
                time_since_last_yield = current_time - last_yield_time
                
                # Decide whether to yield
                should_yield = False
                if not has_yielded_audio and pending_audio_size >= min_chunk_size:
                    # First yield: wait for minimum chunk size
                    should_yield = True
                    has_yielded_audio = True
                elif has_yielded_audio and (pending_audio_size >= min_chunk_size or time_since_last_yield >= min_yield_interval):
                    # Subsequent yields: either enough audio or enough time has passed
                    should_yield = True
                
                if should_yield and pending_chunks:
                    # Concatenate and yield only the new audio chunks
                    new_audio = np.concatenate(pending_chunks)
                    new_duration = len(new_audio) / sample_rate
                    total_duration = sum(len(chunk) for chunk in all_audio_chunks) / sample_rate
                    
                    log_update = log + f"🎵 Streaming: {total_duration:.1f}s generated (chunk {chunk_count})\n"
                    
                    # Yield streaming audio chunk and keep complete_audio as None during streaming
                    yield (sample_rate, new_audio), None, log_update, gr.update(visible=True)
                    
                    # Clear pending chunks after yielding
                    pending_chunks = []
                    last_yield_time = current_time
            
            # Yield any remaining chunks
            if pending_chunks:
                final_new_audio = np.concatenate(pending_chunks)
                total_duration = sum(len(chunk) for chunk in all_audio_chunks) / sample_rate
                log_update = log + f"🎵 Streaming final chunk: {total_duration:.1f}s total\n"
                yield (sample_rate, final_new_audio), None, log_update, gr.update(visible=True)
                has_yielded_audio = True  # Mark that we yielded audio
            
            # Wait for generation to complete (with timeout to prevent hanging)
            generation_thread.join(timeout=5.0)  # Increased timeout to 5 seconds

            # If thread is still alive after timeout, force end
            if generation_thread.is_alive():
                print("Warning: Generation thread did not complete within timeout")
                audio_streamer.end()
                generation_thread.join(timeout=5.0)

            # Clean up
            self.current_streamer = None
            self.is_generating = False
            
            generation_time = time.time() - start_time
            
            # Check if stopped by user
            if self.stop_generation:
                yield None, None, "🛑 Generation stopped by user", gr.update(visible=False)
                return
            
            # Debug logging
            # print(f"Debug: has_received_chunks={has_received_chunks}, chunk_count={chunk_count}, all_audio_chunks length={len(all_audio_chunks)}")
            
            # Check if we received any chunks but didn't yield audio
            if has_received_chunks and not has_yielded_audio and all_audio_chunks:
                # We have chunks but didn't meet the yield criteria, yield them now
                complete_audio = np.concatenate(all_audio_chunks)
                final_duration = len(complete_audio) / sample_rate
                
                final_log = log + f"⏱️ Generation completed in {generation_time:.2f} seconds\n"
                final_log += f"🎵 Final audio duration: {final_duration:.2f} seconds\n"
                final_log += f"📊 Total chunks: {chunk_count}\n"
                final_log += "✨ Generation successful! Complete audio is ready.\n"
                final_log += "💡 Not satisfied? You can regenerate or adjust the CFG scale for different results."
                
                # Yield the complete audio
                yield None, (sample_rate, complete_audio), final_log, gr.update(visible=False)
                return
            
            if not has_received_chunks:
                error_log = log + f"\n❌ Error: No audio chunks were received from the model. Generation time: {generation_time:.2f}s"
                yield None, None, error_log, gr.update(visible=False)
                return
            
            if not has_yielded_audio:
                error_log = log + f"\n❌ Error: Audio was generated but not streamed. Chunk count: {chunk_count}"
                yield None, None, error_log, gr.update(visible=False)
                return

            # Prepare the complete audio
            if all_audio_chunks:
                complete_audio = np.concatenate(all_audio_chunks)
                final_duration = len(complete_audio) / sample_rate
                
                final_log = log + f"⏱️ Generation completed in {generation_time:.2f} seconds\n"
                final_log += f"🎵 Final audio duration: {final_duration:.2f} seconds\n"
                final_log += f"📊 Total chunks: {chunk_count}\n"
                final_log += "✨ Generation successful! Complete audio is ready in the 'Complete Audio' tab.\n"
                final_log += "💡 Not satisfied? You can regenerate or adjust the CFG scale for different results."
                
                # Final yield: Clear streaming audio and provide complete audio
                yield None, (sample_rate, complete_audio), final_log, gr.update(visible=False)
            else:
                final_log = log + "❌ No audio was generated."
                yield None, None, final_log, gr.update(visible=False)

        except gr.Error as e:
            # Handle Gradio-specific errors (like input validation)
            self.is_generating = False
            self.current_streamer = None
            error_msg = f"❌ Input Error: {str(e)}"
            print(error_msg)
            yield None, None, error_msg, gr.update(visible=False)
            
        except Exception as e:
            self.is_generating = False
            self.current_streamer = None
            error_msg = f"❌ An unexpected error occurred: {str(e)}"
            print(error_msg)
            import traceback
            traceback.print_exc()
            yield None, None, error_msg, gr.update(visible=False)
    
    def _generate_with_streamer(self, inputs, cfg_scale, audio_streamer):
        """Helper method to run generation with streamer in a separate thread."""
        try:
            # Check for stop signal before starting generation
            if self.stop_generation:
                audio_streamer.end()
                return
                
            # Define a stop check function that can be called from generate
            def check_stop_generation():
                return self.stop_generation
                
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=None,
                cfg_scale=cfg_scale,
                tokenizer=self.processor.tokenizer,
                generation_config={
                    'do_sample': False,
                },
                audio_streamer=audio_streamer,
                stop_check_fn=check_stop_generation,  # Pass the stop check function
                verbose=False,  # Disable verbose in streaming mode
                refresh_negative=True,
            )
            
        except Exception as e:
            print(f"Error in generation thread: {e}")
            traceback.print_exc()
            # Make sure to end the stream on error
            audio_streamer.end()
    
    def stop_audio_generation(self):
        """Stop the current audio generation process."""
        self.stop_generation = True
        if self.current_streamer is not None:
            try:
                self.current_streamer.end()
            except Exception as e:
                print(f"Error stopping streamer: {e}")
        print("🛑 Audio generation stop requested")
    
    def load_example_scripts(self):
        """Load example scripts from the text_examples directory."""
        examples_dir = os.path.join(os.path.dirname(__file__), "text_examples")
        self.example_scripts = []
        
        # Check if text_examples directory exists
        if not os.path.exists(examples_dir):
            print(f"Warning: text_examples directory not found at {examples_dir}")
            return
        
        # Get all .txt files in the text_examples directory
        txt_files = sorted([f for f in os.listdir(examples_dir) 
                          if f.lower().endswith('.txt') and os.path.isfile(os.path.join(examples_dir, f))])
        
        for txt_file in txt_files:
            file_path = os.path.join(examples_dir, txt_file)
            
            import re
            # Check if filename contains a time pattern like "45min", "90min", etc.
            time_pattern = re.search(r'(\d+)min', txt_file.lower())
            if time_pattern:
                minutes = int(time_pattern.group(1))
                if minutes > 15:
                    print(f"Skipping {txt_file}: duration {minutes} minutes exceeds 15-minute limit")
                    continue

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    script_content = f.read().strip()
                
                # Remove empty lines and lines with only whitespace
                script_content = '\n'.join(line for line in script_content.split('\n') if line.strip())
                
                if not script_content:
                    continue
                
                # Parse the script to determine number of speakers
                num_speakers = self._get_num_speakers_from_script(script_content)
                
                # Add to examples list as [num_speakers, script_content]
                self.example_scripts.append([num_speakers, script_content])
                print(f"Loaded example: {txt_file} with {num_speakers} speakers")
                
            except Exception as e:
                print(f"Error loading example script {txt_file}: {e}")
        
        if self.example_scripts:
            print(f"Successfully loaded {len(self.example_scripts)} example scripts")
        else:
            print("No example scripts were loaded")
    
    def _get_num_speakers_from_script(self, script: str) -> int:
        """Determine the number of unique speakers in a script."""
        import re
        speakers = set()
        
        lines = script.strip().split('\n')
        for line in lines:
            # Use regex to find speaker patterns
            match = re.match(r'^Speaker\s+(\d+)\s*:', line.strip(), re.IGNORECASE)
            if match:
                speaker_id = int(match.group(1))
                speakers.add(speaker_id)
        
        # If no speakers found, default to 1
        if not speakers:
            return 1
        
        # Return the maximum speaker ID + 1 (assuming 0-based indexing)
        # or the count of unique speakers if they're 1-based
        max_speaker = max(speakers)
        min_speaker = min(speakers)
        
        if min_speaker == 0:
            return max_speaker + 1
        else:
            # Assume 1-based indexing, return the count
            return len(speakers)
    

def create_demo_interface(gpu_manager: GPUManager):
    """Create the Gradio interface with streaming support and multi-GPU scheduling."""
    
    # Custom CSS for high-end aesthetics with lighter theme
    custom_css = """
    /* Modern light theme with gradients */
    .gradio-container {
        background: linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%);
        font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    
    /* Header styling */
    .main-header {
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        text-align: center;
        box-shadow: 0 10px 40px rgba(102, 126, 234, 0.3);
    }
    
    .main-header h1 {
        color: white;
        font-size: 2.5rem;
        font-weight: 700;
        margin: 0;
        text-shadow: 0 2px 4px rgba(0,0,0,0.3);
    }
    
    .main-header p {
        color: rgba(255,255,255,0.9);
        font-size: 1.1rem;
        margin: 0.5rem 0 0 0;
    }
    
    /* Card styling */
    .settings-card, .generation-card {
        background: rgba(255, 255, 255, 0.8);
        backdrop-filter: blur(10px);
        border: 1px solid rgba(226, 232, 240, 0.8);
        border-radius: 16px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
    }
    
    /* Speaker selection styling */
    .speaker-grid {
        display: grid;
        gap: 1rem;
        margin-bottom: 1rem;
    }
    
    .speaker-item {
        background: linear-gradient(135deg, #e2e8f0 0%, #cbd5e1 100%);
        border: 1px solid rgba(148, 163, 184, 0.4);
        border-radius: 12px;
        padding: 1rem;
        color: #374151;
        font-weight: 500;
    }
    
    /* Streaming indicator */
    .streaming-indicator {
        display: inline-block;
        width: 10px;
        height: 10px;
        background: #22c55e;
        border-radius: 50%;
        margin-right: 8px;
        animation: pulse 1.5s infinite;
    }
    
    @keyframes pulse {
        0% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.5; transform: scale(1.1); }
        100% { opacity: 1; transform: scale(1); }
    }
    
    /* Queue status styling */
    .queue-status {
        background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
        border: 1px solid rgba(14, 165, 233, 0.3);
        border-radius: 8px;
        padding: 0.75rem;
        margin: 0.5rem 0;
        text-align: center;
        font-size: 0.9rem;
        color: #0369a1;
    }
    
    .generate-btn {
        background: linear-gradient(135deg, #059669 0%, #0d9488 100%);
        border: none;
        border-radius: 12px;
        padding: 1rem 2rem;
        color: white;
        font-weight: 600;
        font-size: 1.1rem;
        box-shadow: 0 4px 20px rgba(5, 150, 105, 0.4);
        transition: all 0.3s ease;
    }
    
    .generate-btn:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 25px rgba(5, 150, 105, 0.6);
    }
    
    .stop-btn {
        background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
        border: none;
        border-radius: 12px;
        padding: 1rem 2rem;
        color: white;
        font-weight: 600;
        font-size: 1.1rem;
        box-shadow: 0 4px 20px rgba(239, 68, 68, 0.4);
        transition: all 0.3s ease;
    }
    
    .stop-btn:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 25px rgba(239, 68, 68, 0.6);
    }
    
    /* Audio player styling */
    .audio-output {
        background: linear-gradient(135deg, #f1f5f9 0%, #e2e8f0 100%);
        border-radius: 16px;
        padding: 1.5rem;
        border: 1px solid rgba(148, 163, 184, 0.3);
    }
    
    .complete-audio-section {
        margin-top: 1rem;
        padding: 1rem;
        background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
        border: 1px solid rgba(34, 197, 94, 0.3);
        border-radius: 12px;
    }
    
    /* Text areas */
    .script-input, .log-output {
        background: rgba(255, 255, 255, 0.9) !important;
        border: 1px solid rgba(148, 163, 184, 0.4) !important;
        border-radius: 12px !important;
        color: #1e293b !important;
        font-family: 'JetBrains Mono', monospace !important;
    }
    
    .script-input::placeholder {
        color: #64748b !important;
    }
    
    /* Sliders */
    .slider-container {
        background: rgba(248, 250, 252, 0.8);
        border: 1px solid rgba(226, 232, 240, 0.6);
        border-radius: 8px;
        padding: 1rem;
        margin: 0.5rem 0;
    }
    
    /* Labels and text */
    .gradio-container label {
        color: #374151 !important;
        font-weight: 600 !important;
    }
    
    .gradio-container .markdown {
        color: #1f2937 !important;
    }
    
    /* Responsive design */
    @media (max-width: 768px) {
        .main-header h1 { font-size: 2rem; }
        .settings-card, .generation-card { padding: 1rem; }
    }
    
    /* Random example button styling - more subtle professional color */
    .random-btn {
        background: linear-gradient(135deg, #64748b 0%, #475569 100%);
        border: none;
        border-radius: 12px;
        padding: 1rem 1.5rem;
        color: white;
        font-weight: 600;
        font-size: 1rem;
        box-shadow: 0 4px 20px rgba(100, 116, 139, 0.3);
        transition: all 0.3s ease;
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    .random-btn:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 25px rgba(100, 116, 139, 0.4);
        background: linear-gradient(135deg, #475569 0%, #334155 100%);
    }
    
    /* GPU status display styling */
    .gpu-status-display {
        background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
        border: 1px solid rgba(14, 165, 233, 0.3);
        border-radius: 12px;
        padding: 1rem;
        margin: 0.5rem 0;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.9rem;
        color: #0369a1;
        white-space: pre-line;
    }
    
    .gpu-status-display h3 {
        margin-top: 0;
        color: #0284c7;
    }
    """
    
    with gr.Blocks(
        title="VibeVoice - AI Podcast Generator",
        css=custom_css,
        theme=gr.themes.Soft(
            primary_hue="blue",
            secondary_hue="purple",
            neutral_hue="slate",
        )
    ) as interface:
        
        # Header
        gr.HTML("""
        <div class="main-header">
            <h1>🎙️ Vibe Podcasting </h1>
            <p>Generating Long-form Multi-speaker AI Podcast with VibeVoice</p>
        </div>
        """)
        
        with gr.Row():
            # Left column - Settings
            with gr.Column(scale=1, elem_classes="settings-card"):
                gr.Markdown("### 🎛️ **Podcast Settings**")
                
                # Number of speakers
                num_speakers = gr.Slider(
                    minimum=1,
                    maximum=4,
                    value=2,
                    step=1,
                    label="Number of Speakers",
                    elem_classes="slider-container"
                )
                
                # Speaker selection
                gr.Markdown("### 🎭 **Speaker Selection**")
                
                # 从第一个GPU实例获取可用声音列表（所有GPU实例应该有相同的声音）
                first_gpu_id = list(gpu_manager.gpu_instances.keys())[0]
                available_speaker_names = list(gpu_manager.gpu_instances[first_gpu_id].available_voices.keys())
                # default_speakers = available_speaker_names[:4] if len(available_speaker_names) >= 4 else available_speaker_names
                default_speakers = ['en-Alice_woman', 'en-Carter_man', 'en-Frank_man', 'en-Maya_woman']

                speaker_selections = []
                for i in range(4):
                    default_value = default_speakers[i] if i < len(default_speakers) else None
                    speaker = gr.Dropdown(
                        choices=available_speaker_names,
                        value=default_value,
                        label=f"Speaker {i+1}",
                        visible=(i < 2),  # Initially show only first 2 speakers
                        elem_classes="speaker-item"
                    )
                    speaker_selections.append(speaker)
                
                # GPU状态显示
                gr.Markdown("### 🖥️ **GPU状态**")
                gpu_status_display = gr.Markdown(
                    value=gpu_manager.get_gpu_status_summary(),
                    elem_classes="gpu-status-display"
                )
                
                # Advanced settings
                gr.Markdown("### ⚙️ **Advanced Settings**")
                
                # Sampling parameters (contains all generation settings)
                with gr.Accordion("Generation Parameters", open=False):
                    cfg_scale = gr.Slider(
                        minimum=1.0,
                        maximum=2.0,
                        value=1.3,
                        step=0.05,
                        label="CFG Scale (Guidance Strength)",
                        # info="Higher values increase adherence to text",
                        elem_classes="slider-container"
                    )
                
            # Right column - Generation
            with gr.Column(scale=2, elem_classes="generation-card"):
                gr.Markdown("### 📝 **Script Input**")
                
                script_input = gr.Textbox(
                    label="Conversation Script",
                    placeholder="""Enter your podcast script here. You can format it as:

Speaker 0: Welcome to our podcast today!
Speaker 1: Thanks for having me. I'm excited to discuss...

Or paste text directly and it will auto-assign speakers.""",
                    lines=12,
                    max_lines=20,
                    elem_classes="script-input"
                )
                
                # Button row with Random Example on the left and Generate on the right
                with gr.Row():
                    # Random example button (now on the left)
                    random_example_btn = gr.Button(
                        "🎲 Random Example",
                        size="lg",
                        variant="secondary",
                        elem_classes="random-btn",
                        scale=1  # Smaller width
                    )
                    
                    # Generate button (now on the right)
                    generate_btn = gr.Button(
                        "🚀 Generate Podcast",
                        size="lg",
                        variant="primary",
                        elem_classes="generate-btn",
                        scale=2  # Wider than random button
                    )
                
                # Stop button
                stop_btn = gr.Button(
                    "🛑 Stop Generation",
                    size="lg",
                    variant="stop",
                    elem_classes="stop-btn",
                    visible=False
                )
                
                # Streaming status indicator
                streaming_status = gr.HTML(
                    value="""
                    <div style="background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%); 
                                border: 1px solid rgba(34, 197, 94, 0.3); 
                                border-radius: 8px; 
                                padding: 0.75rem; 
                                margin: 0.5rem 0;
                                text-align: center;
                                font-size: 0.9rem;
                                color: #166534;">
                        <span class="streaming-indicator"></span>
                        <strong>LIVE STREAMING</strong> - Audio is being generated in real-time
                    </div>
                    """,
                    visible=False,
                    elem_id="streaming-status"
                )
                
                # Output section
                gr.Markdown("### 🎵 **Generated Podcast**")
                
                # Streaming audio output (outside of tabs for simpler handling)
                audio_output = gr.Audio(
                    label="Streaming Audio (Real-time)",
                    type="numpy",
                    elem_classes="audio-output",
                    streaming=True,  # Enable streaming mode
                    autoplay=True,
                    show_download_button=False,  # Explicitly show download button
                    visible=True
                )
                
                # Complete audio output (non-streaming)
                complete_audio_output = gr.Audio(
                    label="Complete Podcast (Download after generation)",
                    type="numpy",
                    elem_classes="audio-output complete-audio-section",
                    streaming=False,  # Non-streaming mode
                    autoplay=False,
                    show_download_button=True,  # Explicitly show download button
                    visible=False  # Initially hidden, shown when audio is ready
                )
                
                gr.Markdown("""
                *💡 **Streaming**: Audio plays as it's being generated (may have slight pauses)  
                *💡 **Complete Audio**: Will appear below after generation finishes*
                """)
                
                # Generation log
                log_output = gr.Textbox(
                    label="Generation Log",
                    lines=8,
                    max_lines=15,
                    interactive=False,
                    elem_classes="log-output"
                )
        
        def update_speaker_visibility(num_speakers):
            updates = []
            for i in range(4):
                updates.append(gr.update(visible=(i < num_speakers)))
            return updates
        
        num_speakers.change(
            fn=update_speaker_visibility,
            inputs=[num_speakers],
            outputs=speaker_selections
        )
        
        # Main generation function with streaming
        def generate_podcast_wrapper(num_speakers, script, *speakers_and_params):
            """Wrapper function to handle the streaming generation call."""
            try:
                # Extract speakers and parameters
                speakers = speakers_and_params[:4]  # First 4 are speaker selections
                cfg_scale = speakers_and_params[4]   # CFG scale
                
                # Clear outputs and reset visibility at start
                yield None, gr.update(value=None, visible=False), "🎙️ Starting generation...", gr.update(visible=True), gr.update(visible=False), gr.update(visible=True)
                
                # The generator will yield multiple times
                final_log = "Starting generation..."
                
                for streaming_audio, complete_audio, log, streaming_visible in gpu_manager.generate_podcast_streaming(
                    num_speakers=int(num_speakers),
                    script=script,
                    speaker_1=speakers[0],
                    speaker_2=speakers[1],
                    speaker_3=speakers[2],
                    speaker_4=speakers[3],
                    cfg_scale=cfg_scale
                ):
                    final_log = log
                    
                    # Check if we have complete audio (final yield)
                    if complete_audio is not None:
                        # Final state: clear streaming, show complete audio
                        yield None, gr.update(value=complete_audio, visible=True), log, gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)
                    else:
                        # Streaming state: update streaming audio only
                        if streaming_audio is not None:
                            yield streaming_audio, gr.update(visible=False), log, streaming_visible, gr.update(visible=False), gr.update(visible=True)
                        else:
                            # No new audio, just update status
                            yield None, gr.update(visible=False), log, streaming_visible, gr.update(visible=False), gr.update(visible=True)

            except Exception as e:
                error_msg = f"❌ A critical error occurred in the wrapper: {str(e)}"
                print(error_msg)
                import traceback
                traceback.print_exc()
                # Reset button states on error
                yield None, gr.update(value=None, visible=False), error_msg, gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)
        
        def stop_generation_handler():
            """Handle stopping generation."""
            gpu_manager.stop_audio_generation()
            # Return values for: log_output, streaming_status, generate_btn, stop_btn
            return "🛑 Generation stopped.", gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)
        
        # Add a clear audio function
        def clear_audio_outputs():
            """Clear both audio outputs before starting new generation."""
            return None, gr.update(value=None, visible=False)

        # Connect generation button with streaming outputs
        generate_btn.click(
            fn=clear_audio_outputs,
            inputs=[],
            outputs=[audio_output, complete_audio_output],
            queue=False
        ).then(
            fn=generate_podcast_wrapper,
            inputs=[num_speakers, script_input] + speaker_selections + [cfg_scale],
            outputs=[audio_output, complete_audio_output, log_output, streaming_status, generate_btn, stop_btn],
            queue=True  # Enable Gradio's built-in queue
        )
        
        # Connect stop button
        stop_btn.click(
            fn=stop_generation_handler,
            inputs=[],
            outputs=[log_output, streaming_status, generate_btn, stop_btn],
            queue=False  # Don't queue stop requests
        ).then(
            # Clear both audio outputs after stopping
            fn=lambda: (None, None),
            inputs=[],
            outputs=[audio_output, complete_audio_output],
            queue=False
        )
        
        # Function to randomly select an example
        def load_random_example():
            """Randomly select and load an example script."""
            import random
            
            # Get available examples from the first GPU instance
            first_gpu_id = list(gpu_manager.gpu_instances.keys())[0]
            first_demo_instance = gpu_manager.gpu_instances[first_gpu_id]
            if hasattr(first_demo_instance, 'example_scripts') and first_demo_instance.example_scripts:
                example_scripts = first_demo_instance.example_scripts
            else:
                # Fallback to default
                example_scripts = [
                    [2, "Speaker 0: Welcome to our AI podcast demonstration!\nSpeaker 1: Thanks for having me. This is exciting!"]
                ]
            
            # Randomly select one
            if example_scripts:
                selected = random.choice(example_scripts)
                num_speakers_value = selected[0]
                script_value = selected[1]
                
                # Return the values to update the UI
                return num_speakers_value, script_value
            
            # Default values if no examples
            return 2, ""
        
        # Connect random example button
        random_example_btn.click(
            fn=load_random_example,
            inputs=[],
            outputs=[num_speakers, script_input],
            queue=False  # Don't queue this simple operation
        )
        
        # 定期更新GPU状态显示
        def update_gpu_status():
            """更新GPU状态显示"""
            return gpu_manager.get_gpu_status_summary()
        
        # 每10秒自动更新GPU状态
        interface.load(
            fn=update_gpu_status,
            inputs=[],
            outputs=[gpu_status_display],
            every=10  # 每10秒更新一次
        )
        
        # Add usage tips
        gr.Markdown("""
        ### 💡 **Usage Tips**
        
        - Click **🚀 Generate Podcast** to start audio generation
        - **Live Streaming** tab shows audio as it's generated (may have slight pauses)
        - **Complete Audio** tab provides the full, uninterrupted podcast after generation
        - During generation, you can click **🛑 Stop Generation** to interrupt the process
        - The streaming indicator shows real-time generation progress
        """)
        
        # Add example scripts
        gr.Markdown("### 📚 **Example Scripts**")
        
        # Use dynamically loaded examples if available, otherwise provide a default
        first_gpu_id = list(gpu_manager.gpu_instances.keys())[0]
        first_demo_instance = gpu_manager.gpu_instances[first_gpu_id]
        if hasattr(first_demo_instance, 'example_scripts') and first_demo_instance.example_scripts:
            example_scripts = first_demo_instance.example_scripts
        else:
            # Fallback to a simple default example if no scripts loaded
            example_scripts = [
                [1, "Speaker 1: Welcome to our AI podcast demonstration! This is a sample script showing how VibeVoice can generate natural-sounding speech."]
            ]
        
        gr.Examples(
            examples=example_scripts,
            inputs=[num_speakers, script_input],
            label="Try these example scripts:"
        )

    return interface


def convert_to_16_bit_wav(data):
    # Check if data is a tensor and move to cpu
    if torch.is_tensor(data):
        data = data.detach().cpu().numpy()
    
    # Ensure data is numpy array
    data = np.array(data)

    # Normalize to range [-1, 1] if it's not already
    if np.max(np.abs(data)) > 1.0:
        data = data / np.max(np.abs(data))
    
    # Scale to 16-bit integer range
    data = (data * 32767).astype(np.int16)
    return data


def parse_args():
    parser = argparse.ArgumentParser(description="VibeVoice Gradio Demo")
    parser.add_argument(
        "--model_path",
        type=str,
        default="/tmp/vibevoice-model",
        help="Path to the VibeVoice model directory",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda" if torch.cuda.is_available() else "cpu",
        help="Device for inference",
    )
    parser.add_argument(
        "--inference_steps",
        type=int,
        default=10,
        help="Number of inference steps for DDPM (not exposed to users)",
    )
    parser.add_argument(
        "--share",
        action="store_true",
        help="Share the demo publicly via Gradio",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7860,
        help="Port to run the demo on",
    )
    parser.add_argument(
        "--gpus",
        type=str,
        default=None,
        help="Comma-separated list of GPU IDs to use (e.g., '0,1,2'). If not specified, all available GPUs will be used.",
    )
    
    return parser.parse_args()


def main():
    """Main function to run the demo with multi-GPU support."""
    args = parse_args()
    
    set_seed(42)  # Set a fixed seed for reproducibility

    print("🎙️ Initializing VibeVoice Demo with Multi-GPU Support...")
    
    # 解析GPU参数
    target_gpus = None
    if args.gpus:
        try:
            target_gpus = [int(gpu_id.strip()) for gpu_id in args.gpus.split(',')]
            print(f"指定使用GPU: {target_gpus}")
        except ValueError:
            print("❌ 错误: GPU参数格式无效，应为逗号分隔的整数 (例如: '0,1,2')")
            sys.exit(1)
    
    # Initialize GPU manager
    gpu_manager = GPUManager(
        model_path=args.model_path,
        inference_steps=args.inference_steps,
        gpu_ids=target_gpus
    )
    
    # Create interface
    interface = create_demo_interface(gpu_manager)
    
    print(f"🚀 Launching demo on port {args.port}")
    print(f"📁 Model path: {args.model_path}")
    print(f"🖥️ Available GPUs: {len(gpu_manager.gpu_instances)}")
    
    # Print GPU information
    for gpu_id, status in gpu_manager.gpu_status.items():
        status_icon = "✅" if status.is_available else "❌"
        print(f"   {status_icon} GPU {gpu_id}: {status.device_name} ({status.memory_total:.1f}GB)")
    
    first_gpu_id = list(gpu_manager.gpu_instances.keys())[0]
    first_demo_instance = gpu_manager.gpu_instances[first_gpu_id]
    print(f"🎭 Available voices: {len(first_demo_instance.available_voices)}")
    print(f"🔴 Streaming mode: ENABLED")
    print(f"🔒 Multi-GPU scheduling: ENABLED")
    
    # Launch the interface
    try:
        interface.queue(
            max_size=50,  # Increased queue size for multi-GPU
            default_concurrency_limit=len(gpu_manager.gpu_instances)  # Allow concurrent requests equal to GPU count
        ).launch(
            share=args.share,
            # server_port=args.port,
            server_name="0.0.0.0" if args.share else "127.0.0.1",
            show_error=True,
            show_api=False  # Hide API docs for cleaner interface
        )
    except KeyboardInterrupt:
        print("\n🛑 Shutting down gracefully...")
        gpu_manager.shutdown()
    except Exception as e:
        print(f"❌ Server error: {e}")
        gpu_manager.shutdown()
        raise


if __name__ == "__main__":
    main()
