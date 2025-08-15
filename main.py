# tts_client.py (V5.0 - 流式处理版)
# 核心架构变更：客户端现在接收音频块数据包，并在本地进行重组和拼接，再放入播放队列。
import collections, random, re, time, os, shutil, threading, queue, json, struct, socket, sys, base64
import subprocess
from pydub import AudioSegment

class TTSClientGenerator:
    def __init__(self, server_host, server_port, output_dir, **kwargs):
        self.server_host = server_host
        self.server_port = server_port
        self.sock = None
        self.lock = threading.Lock()
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 【核心改动】音频块重组缓冲区
        self.reassembly_buffer = {} #格式: {req_id: {"chunks": {}, "total": N, "received_time": T}}
        self.play_queue = queue.PriorityQueue()
        self._stop_event = threading.Event()
        self.current_playback_process = None

        self.log(f"流式TTS客户端初始化，准备连接服务器 {self.server_host}:{self.server_port}")
        self._connect_to_server()

        # 启动独立的网络监听和播放线程
        self.network_thread = threading.Thread(target=self.network_listener_worker, daemon=True)
        self.play_audio_thread = threading.Thread(target=self.play_audio_worker, daemon=True)
        self.network_thread.start()
        self.play_audio_thread.start()

    def log(self, message):
        print(f"[TTSClient] {message}")

    def add_task(self, text, priority=2):
        """主程序调用的唯一入口：发送任务请求到服务器"""
        with self.lock:
            if not self.sock:
                if not self._connect_to_server():
                    self.log(f"任务发送失败，无法连接服务器: '{text[:30]}...'")
                    return
            try:
                request_packet = {
                    "text": text,
                    "priority": priority
                }
                self._send_msg(json.dumps(request_packet).encode('utf-8'))
                self.log(f"已发送任务 (Prio:{priority}): '{text[:30]}...'")
            except Exception as e:
                self.log(f"发送任务时出错: {e}")
                self.sock = None

    def network_listener_worker(self):
        """专门负责接收和处理服务器发来的所有数据块"""
        while not self._stop_event.is_set():
            if not self.sock:
                time.sleep(2) # 如果未连接，稍等后重试
                continue
            try:
                raw_data = self._recv_msg()
                if raw_data is None:
                    self.log("与服务器连接断开，将尝试重连...")
                    self.sock = None
                    continue
                
                packet = json.loads(raw_data.decode('utf-8'))
                
                if packet.get("status") == "error":
                    self.log(f"收到服务器错误包: ReqID {packet.get('request_id')}, Chunk {packet.get('chunk_id')}, Msg: {packet.get('message')}")
                    continue

                req_id = packet['request_id']
                
                # 将数据块存入缓冲区
                if req_id not in self.reassembly_buffer:
                    self.reassembly_buffer[req_id] = {
                        "chunks": {},
                        "total": packet['total_chunks'],
                        "received_time": time.time(),
                        "priority": packet['priority'],
                        "full_text": "" # 用于拼接完整文本
                    }
                
                # 解码并存储音频块
                audio_data = base64.b64decode(packet['audio_data'])
                self.reassembly_buffer[req_id]["chunks"][packet['chunk_id']] = (audio_data, packet['text'])
                
                # 检查是否已收齐所有块
                buffer_entry = self.reassembly_buffer[req_id]
                if len(buffer_entry["chunks"]) == buffer_entry["total"]:
                    self.log(f"ReqID {req_id} 的所有 {buffer_entry['total']} 个音频块已收齐，准备拼接。")
                    self._assemble_and_queue_audio(req_id)

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                self.log("与服务器连接中断，将尝试重连...")
                self.sock = None
            except Exception as e:
                self.log(f"网络监听线程出错: {e}")

    def _assemble_and_queue_audio(self, req_id):
        """拼接指定ID的音频块并放入播放队列"""
        buffer_entry = self.reassembly_buffer.pop(req_id)
        
        try:
            combined_audio = AudioSegment.empty()
            full_text_parts = []
            # 按chunk_id排序并拼接
            sorted_chunks = sorted(buffer_entry["chunks"].items())
            
            for chunk_id, (audio_data, text_part) in sorted_chunks:
                temp_chunk_path = os.path.join(self.output_dir, f"chunk_{req_id}_{chunk_id}.wav")
                with open(temp_chunk_path, 'wb') as f:
                    f.write(audio_data)
                
                segment = AudioSegment.from_file(temp_chunk_path)
                combined_audio += segment
                full_text_parts.append(text_part)
                os.remove(temp_chunk_path)

            final_path = os.path.join(self.output_dir, f"final_audio_{req_id}.wav")
            combined_audio.export(final_path, format="wav")
            
            full_text = "".join(full_text_parts)
            self.play_queue.put((buffer_entry["priority"], req_id, final_path, full_text))
            self.log(f"✅ ReqID {req_id} 音频拼接完成并放入播放队列。")

        except Exception as e:
            self.log(f"❌ 拼接 ReqID {req_id} 音频时失败: {e}")

    def play_audio_worker(self):
        """播放队列中的完整音频，此逻辑基本不变"""
        while not self._stop_event.is_set():
            try:
                priority, req_id, audio_path, content = self.play_queue.get(timeout=1)
                
                self.log(f"正在播放任务 ReqID:{req_id} (Prio:{priority}): '{content[:50]}...'")
                self._play_audio_in_subprocess(audio_path)

                if os.path.exists(audio_path):
                    try: os.remove(audio_path)
                    except OSError: pass

                self.play_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                self.log(f"❌ 音频播放工作线程出错: {e}")

    # ... 其他辅助函数 ...
    def stop(self):
        self._stop_event.set()
        if self.current_playback_process and self.current_playback_process.poll() is None:
            self.current_playback_process.terminate()
        with self.lock:
            if self.sock:
                self.sock.close()
                self.sock = None

    def _play_audio_in_subprocess(self, audio_path):
        # ... 此函数逻辑不变 ...
        if self.current_playback_process and self.current_playback_process.poll() is None:
            self.current_playback_process.terminate()
            try: self.current_playback_process.wait(timeout=0.5)
            except subprocess.TimeoutExpired: pass
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            player_script_path = os.path.join(base_dir, "local_model_client.py")
            if not os.path.exists(player_script_path):
                self.log(f"❌ 致命错误：找不到播放脚本 'local_model_client.py'")
                return
            command = [sys.executable, player_script_path, os.path.abspath(audio_path)]
            self.current_playback_process = subprocess.Popen(command, cwd=base_dir, creationflags=subprocess.CREATE_NO_WINDOW)
            self.current_playback_process.wait()
        except Exception as e:
            self.log(f"❌ 启动播放子进程时出错: {e}")

    def _connect_to_server(self):
        with self.lock:
            if self.sock: self.sock.close()
            try:
                self.log("正在连接到TTS中央服务器...")
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.connect((self.server_host, self.server_port))
                self.log("✅ 成功连接到TTS服务器。")
                return True
            except Exception as e:
                self.log(f"❌ 连接TTS服务器失败: {e}")
                self.sock = None
                return False

    def _send_msg(self, data):
        length = struct.pack('>I', len(data))
        self.sock.sendall(length + data)
    def _recv_msg(self):
        raw_msglen = self._recv_all(4)
        if not raw_msglen: return None
        msglen = struct.unpack('>I', raw_msglen)[0]
        return self._recv_all(msglen)
    def _recv_all(self, n):
        data = bytearray()
        while len(data) < n:
            packet = self.sock.recv(n - len(data))
            if not packet: return None
            data.extend(packet)
        return data
