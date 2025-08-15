# tts_client.py (V5.2 - 全功能修复版)
# 核心架构变更：客户端现在接收音频块数据包，并在本地进行重组和拼接，再放入播放队列。
# 本次更新：恢复了 interrupt_and_speak, get_unprocessed_size, can_generate_new_script, test_and_play_sync 及本地音效功能。
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
        
        # 【修复】重新加入本地音效模块
        self.sounds_path = "sounds"
        self.sounds_library = {"[咳嗽]": "咳嗽声.WAV", "[叹气]": "叹气声.WAV", "[吞咽]": "吞咽声.WAV",
                               "[呼吸]": ["呼吸1.WAV", "呼吸2.WAV", "呼吸3.WAV"]}
        
        self.reassembly_buffer = {} #格式: {req_id: {"chunks": {}, "total": N, "received_time": T}}
        self.play_queue = queue.PriorityQueue()
        self._stop_event = threading.Event()
        self.current_playback_process = None

        self.log(f"流式TTS客户端初始化，准备连接服务器 {self.server_host}:{self.server_port}")
        self._connect_to_server()

        self.network_thread = threading.Thread(target=self.network_listener_worker, daemon=True)
        self.play_audio_thread = threading.Thread(target=self.play_audio_worker, daemon=True)
        self.network_thread.start()
        self.play_audio_thread.start()

    def log(self, message):
        print(f"[TTSClient] {message}")

    def add_task(self, text, priority=2):
        """主程序调用的唯一入口：发送任务请求到服务器"""
        # 【修复】增加本地音效处理逻辑
        if text in self.sounds_library:
            self._queue_local_sound(text, priority)
            return
            
        with self.lock:
            if not self.sock:
                if not self._connect_to_server():
                    self.log(f"任务发送失败，无法连接服务器: '{text[:30]}...'")
                    return
            try:
                request_packet = {
                    "text": text,
                    "priority": priority,
                    "request_type": "default" # 明确告知是常规任务
                }
                self._send_msg(json.dumps(request_packet).encode('utf-8'))
                self.log(f"已发送任务 (Prio:{priority}): '{text[:30]}...'")
            except Exception as e:
                self.log(f"发送任务时出错: {e}")
                self.sock = None

    # 【修复】重新加入 interrupt_and_speak 函数
    def interrupt_and_speak(self, text):
        """紧急插话功能。通过发送一个最高优先级的任务来实现插队。"""
        self.log(f"⚡ 收到紧急插话指令: {text}")
        # 使用优先级 0 来确保服务器优先处理
        self.add_task(text, priority=0)

    # 【修复】重新加入 get_unprocessed_size 函数
    def get_unprocessed_size(self):
        """返回正在重组和等待播放的任务总数，用于判断是否繁忙"""
        return self.play_queue.qsize() + len(self.reassembly_buffer)

    # 【修复】重新加入 can_generate_new_script 函数
    def can_generate_new_script(self):
        """判断是否可以生成新话术的客户端近似逻辑"""
        # 如果等待处理（重组+播放）的任务少于5个，就认为可以生成
        return self.get_unprocessed_size() < 5

    # 【修复】重新加入 test_and_play_sync 函数
    def test_and_play_sync(self, text, use_assistant=False):
        """发送一个专门的测试请求到服务器"""
        self.log(f"【声音测试】{'助播' if use_assistant else '主线'}: {text}")
        if not text: return
        
        request_type = 'test_assistant' if use_assistant else 'test_main'
        # 使用最高优先级(-1)发送测试请求
        self.generate_audio_via_network(text, -1, request_type=request_type)
        self.log("【声音测试】请求已发送到服务器，请等待播放...")

    def generate_audio_via_network(self, text, priority, request_type='default'):
        # 这个函数现在是内部调用，负责实际的网络发送
        with self.lock:
            if not self.sock and not self._connect_to_server():
                self.log(f"网络请求发送失败，无法连接服务器。")
                return
            try:
                request_data = json.dumps({
                    'text': text,
                    'request_type': request_type,
                    'priority': priority
                }).encode('utf-8')
                self._send_msg(request_data)
            except Exception as e:
                self.log(f"发送网络请求时出错: {e}")
                self.sock = None


    def network_listener_worker(self):
        # ... 此函数逻辑与上一版相同 ...
        while not self._stop_event.is_set():
            if not self.sock:
                time.sleep(2)
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
                if req_id not in self.reassembly_buffer:
                    self.reassembly_buffer[req_id] = {
                        "chunks": {}, "total": packet['total_chunks'], "received_time": time.time(),
                        "priority": packet['priority'], "full_text": ""
                    }
                audio_data = base64.b64decode(packet['audio_data'])
                self.reassembly_buffer[req_id]["chunks"][packet['chunk_id']] = (audio_data, packet['text'])
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
        # ... 此函数逻辑与上一版相同 ...
        buffer_entry = self.reassembly_buffer.pop(req_id)
        try:
            combined_audio = AudioSegment.empty()
            full_text_parts = []
            sorted_chunks = sorted(buffer_entry["chunks"].items())
            for chunk_id, (audio_data, text_part) in sorted_chunks:
                temp_chunk_path = os.path.join(self.output_dir, f"chunk_{req_id}_{chunk_id}.wav")
                with open(temp_chunk_path, 'wb') as f: f.write(audio_data)
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
        while not self._stop_event.is_set():
            try:
                priority, req_id, audio_path, content = self.play_queue.get(timeout=1)
                self.log(f"正在播放任务 ReqID:{req_id} (Prio:{priority}): '{content[:50]}...'")
                self._play_audio_in_subprocess(audio_path)
                # 【修复】本地音效文件不应被删除
                if os.path.exists(audio_path) and not audio_path.startswith(self.sounds_path):
                    try: os.remove(audio_path)
                    except OSError: pass
                self.play_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                self.log(f"❌ 音频播放工作线程出错: {e}")

    # 【新增】处理本地音效的函数
    def _queue_local_sound(self, command, priority):
        sound_info = self.sounds_library.get(command)
        if not sound_info: return
        filename = random.choice(sound_info) if isinstance(sound_info, list) else sound_info
        audio_path = os.path.join(self.sounds_path, filename)
        if os.path.exists(audio_path):
            local_req_id = -int(time.time()) # 使用负数时间戳作为唯一ID
            self.play_queue.put((priority, local_req_id, audio_path, command))
            self.log(f"✅ 本地音效已入队: {command}")
        else:
            self.log(f"❌ 本地音效文件未找到: {audio_path}")

    def stop(self):
        # ... 此函数逻辑与上一版相同 ...
        self._stop_event.set()
        if self.current_playback_process and self.current_playback_process.poll() is None:
            self.current_playback_process.terminate()
        with self.lock:
            if self.sock:
                self.sock.close()
                self.sock = None

    def _play_audio_in_subprocess(self, audio_path):
        # ... 此函数逻辑与上一版相同 ...
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
        # ... 此函数逻辑与上一版相同 ...
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
        # ... 此函数逻辑与上一版相同 ...
        length = struct.pack('>I', len(data))
        self.sock.sendall(length + data)
    def _recv_msg(self):
        # ... 此函数逻辑与上一版相同 ...
        raw_msglen = self._recv_all(4)
        if not raw_msglen: return None
        msglen = struct.unpack('>I', raw_msglen)[0]
        return self._recv_all(msglen)
    def _recv_all(self, n):
        # ... 此函数逻辑与上一版相同 ...
        data = bytearray()
        while len(data) < n:
            packet = self.sock.recv(n - len(data))
            if not packet: return None
            data.extend(packet)
        return data
