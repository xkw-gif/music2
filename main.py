# tts_client.py (V9.2 - 真正流式、无拼接播放最终版)
# 核心架构：客户端分割文本，向服务器发送块请求。接收到音频块后，不拼接，
# 直接将带有完整序列号的音频块放入优先队列，实现按顺序的流式播放。
import collections, random, re, time, os, shutil, threading, queue, json, struct, socket, sys, base64
import subprocess

class TTSClientGenerator:
    def __init__(self, server_host, server_port, output_dir, now_playing_callback=None, **kwargs):
        self.server_host = server_host
        self.server_port = server_port
        self.sock = None
        self.lock = threading.Lock()
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.sounds_path = "sounds"
        self.sounds_library = {"[咳嗽]": "咳嗽声.WAV", "[叹气]": "叹气声.WAV", "[吞咽]": "吞咽声.WAV",
                               "[呼吸]": ["呼吸1.WAV", "呼吸2.WAV", "呼吸3.WAV"]}
        
        self.keyword_responses = kwargs.get('keyword_responses', {})
        self.sensitive_words = kwargs.get('sensitive_words', [])
        self.now_playing_callback = now_playing_callback
        
        # 播放队列，现在直接存放音频块
        self.play_queue = queue.PriorityQueue()
        self._stop_event = threading.Event()
        self.current_playback_process = None
        self.request_id_counter = 0
        self.pending_chunk_count = 0 # 跟踪正在网络中或服务器上处理的块数量

        self.log(f"流式TTS客户端初始化，准备连接服务器 {self.server_host}:{self.server_port}")
        self._connect_to_server()

        self.network_thread = threading.Thread(target=self.network_listener_worker, daemon=True, name="NetworkListener")
        self.play_audio_thread = threading.Thread(target=self.play_audio_worker, daemon=True, name="AudioPlayer")
        self.network_thread.start()
        self.play_audio_thread.start()

    def log(self, message):
        print(f"[TTSClient] {message}")

    def _get_next_request_id(self):
        self.request_id_counter += 1
        return self.request_id_counter

    def _split_text(self, text):
        sentences = [s.strip() for s in re.split(r'([？！。.~…\n])', text) if s.strip()]
        merged = []
        temp = ""
        for item in sentences:
            if item in '？！。.~…\n':
                temp += item
                merged.append(temp)
                temp = ""
            else:
                temp += item
        if temp: merged.append(temp)
        return merged

    def _filter_sensitive(self, text):
        for word in self.sensitive_words:
            text = text.replace(word, "**")
        return text

    def _send_chunk_request(self, payload):
        with self.lock:
            if not self.sock and not self._connect_to_server():
                self.log(f"音频块请求发送失败，无法连接服务器。")
                self.pending_chunk_count = max(0, self.pending_chunk_count - 1)
                return
            try:
                request_data = json.dumps(payload).encode('utf-8')
                self._send_msg(request_data)
            except Exception as e:
                self.log(f"发送音频块请求时出错: {e}")
                self.pending_chunk_count = max(0, self.pending_chunk_count - 1)
                self.sock = None

    def add_task(self, text, priority=2, request_type='default'):
        if text in self.sounds_library:
            self._queue_local_sound(text, priority)
            return

        filtered_text = self._filter_sensitive(text)
        request_id = self._get_next_request_id()
        chunks = []
        
        if len(filtered_text) > 100:
            chunks = self._split_text(filtered_text)
        else:
            chunks = [filtered_text]

        if not chunks: return
            
        total_chunks = len(chunks)
        self.log(f"新任务 ReqID:{request_id} (Prio:{priority}) 被分割成 {total_chunks} 块，开始发送请求...")

        for i, chunk_text in enumerate(chunks):
            chunk_id = i + 1
            clean_chunk = chunk_text.replace(" ", "").replace("\n", "")
            if not clean_chunk: continue

            self.pending_chunk_count += 1 # 每发送一个请求，计数器加一
            triggered_keyword = next((kw for kw in self.keyword_responses if kw in clean_chunk), None)
            
            payload = {
                "request_id": request_id, "chunk_id": chunk_id, "total_chunks": total_chunks,
                "priority": priority, "text": clean_chunk, "request_type": request_type,
                "triggered_keyword": triggered_keyword
            }
            threading.Thread(target=self._send_chunk_request, args=(payload,)).start()

    def interrupt_and_speak(self, text):
        self.log(f"⚡ 收到紧急插话指令: {text}")
        self.add_task(text, priority=0)

    def get_unprocessed_size(self):
        return self.play_queue.qsize() + self.pending_chunk_count

    def can_generate_new_script(self):
        return self.get_unprocessed_size() < 5

    def test_and_play_sync(self, text, use_assistant=False):
        self.log(f"【声音测试】{'助播' if use_assistant else '主线'}: {text}")
        if not text: return
        request_type = 'test_assistant' if use_assistant else 'test_main'
        self.add_task(text, priority=-1, request_type=request_type)

    def network_listener_worker(self):
        """持续监听并接收服务器返回的音频块，直接放入播放队列"""
        while not self._stop_event.is_set():
            if not self.sock:
                time.sleep(1)
                continue
            try:
                response_data = self._recv_msg()
                if response_data is None:
                    self.log("与服务器连接断开，将尝试重连...")
                    self.sock = None
                    continue
                
                packet = json.loads(response_data.decode('utf-8'))
                
                self.pending_chunk_count = max(0, self.pending_chunk_count - 1) # 收到响应，计数器减一

                if packet.get("status") == "error":
                    self.log(f"收到服务器错误包: ReqID {packet.get('request_id')}, Chunk {packet.get('chunk_id')}")
                    continue

                audio_data = base64.b64decode(packet['audio_data'])
                req_id = packet['request_id']
                chunk_id = packet['chunk_id']
                
                temp_audio_path = os.path.join(self.output_dir, f"audio_{req_id}_{chunk_id}.wav")
                with open(temp_audio_path, 'wb') as f:
                    f.write(audio_data)
                
                # 【核心改动】将单个音频块直接放入播放队列，用 (priority, request_id, chunk_id) 来排序
                self.play_queue.put((packet['priority'], req_id, chunk_id, temp_audio_path, packet['text']))
                self.log(f"✅ 已接收并入队音频块 {chunk_id}/{packet['total_chunks']} (ReqID: {req_id})")

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                self.log("与服务器连接中断，将尝试重连...")
                self.sock = None
            except Exception as e:
                self.log(f"网络监听线程出错: {e}")

    def play_audio_worker(self):
        """按顺序播放队列中的音频块"""
        current_req_id = -1
        full_text_for_ui = ""
        
        while not self._stop_event.is_set():
            try:
                # 队列现在是 (priority, request_id, chunk_id, audio_path, content)
                priority, req_id, chunk_id, audio_path, content = self.play_queue.get(timeout=1)
                
                # UI同步逻辑：如果是新的一句话，先更新UI；如果是同一句话的后续部分，则不更新
                if req_id != current_req_id:
                    current_req_id = req_id
                    # 查找这句话的所有文本并拼接，用于UI显示
                    # 这是一个近似逻辑，因为队列中可能没有所有块
                    # 但在播放第一块时，可以先显示第一块的文本
                    if self.now_playing_callback:
                        self.now_playing_callback(content)
                
                self.log(f"正在播放块 ReqID:{req_id}-{chunk_id} (Prio:{priority}): '{content}'")
                self._play_audio_in_subprocess(audio_path)
                
                if os.path.exists(audio_path) and not audio_path.startswith(self.sounds_path):
                    try: os.remove(audio_path)
                    except OSError: pass
                
                self.play_queue.task_done()
            except queue.Empty:
                current_req_id = -1 # 队列为空，重置当前句子ID
                if self.now_playing_callback:
                    self.now_playing_callback("") # 清空UI显示
                continue
            except Exception as e:
                self.log(f"❌ 音频播放工作线程出错: {e}")

    def _queue_local_sound(self, command, priority):
        if self.now_playing_callback:
            self.now_playing_callback(command)
        sound_info = self.sounds_library.get(command)
        if not sound_info: return
        filename = random.choice(sound_info) if isinstance(sound_info, list) else sound_info
        audio_path = os.path.join(self.sounds_path, filename)
        if os.path.exists(audio_path):
            req_id = self._get_next_request_id()
            self.play_queue.put((priority, req_id, 1, audio_path, command))
            self.log(f"✅ 本地音效已入队: {command}")
        else:
            self.log(f"❌ 本地音效文件未找到: {audio_path}")

    def stop(self):
        self._stop_event.set()
        if self.current_playback_process and self.current_playback_process.poll() is None:
            self.current_playback_process.terminate()
        with self.lock:
            if self.sock:
                self.sock.close()
                self.sock = None

    def _play_audio_in_subprocess(self, audio_path):
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
