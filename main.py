# tts_client.py (V10.0 - 完整功能最终版)
# 核心架构：客户端完整复刻 tts_text.py 的所有功能逻辑，包括文本预处理、任务跟踪、
# 关键词检测、AI助播调用、音频拼接等。仅将语音合成步骤替换为对服务器的网络请求。
import collections, random, re, time, os, shutil, threading, queue, json, struct, socket, sys, base64
import subprocess
from pydub import AudioSegment
from ali import AIResponseGenerator # 客户端需要自己调用AI

class TTSClientGenerator:
    def __init__(self, server_host, server_port, output_dir, now_playing_callback=None, **kwargs):
        # 网络配置
        self.server_host = server_host
        self.server_port = server_port
        self.sock = None
        self.lock = threading.Lock() # 用于保护socket连接
        
        # 本地文件与路径配置
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self.sounds_path = "sounds"
        self.sounds_library = {"[咳嗽]": "咳嗽声.WAV", "[叹气]": "叹气声.WAV", "[吞咽]": "吞咽声.WAV",
                               "[呼吸]": ["呼吸1.WAV", "呼吸2.WAV", "呼吸3.WAV"]}
        
        # 【核心恢复】所有功能模块和状态变量
        self.keyword_responses = kwargs.get('keyword_responses', {})
        self.sensitive_words = kwargs.get('sensitive_words', [])
        self.ai_generator = kwargs.get('ai_generator') # 从core.py传入AI实例
        self.now_playing_callback = now_playing_callback
        
        self.play_queue = queue.PriorityQueue()
        self.play_lock = threading.Lock() # 播放锁
        
        # 任务跟踪
        self.seq = 0
        self.pending_priority1 = 0
        self.pending_priority2 = 0
        self.auto_task_ids = collections.deque(maxlen=5)
        self.cancelled_auto_tasks = set()
        
        # 音频块重组缓冲区
        self.reassembly_buffer = {} # 格式: {seq: {"main": data, "assist": data, "content": text, "priority": p}}

        self._stop_event = threading.Event()
        self.current_playback_process = None

        self.log(f"TTS客户端初始化，准备连接服务器 {self.server_host}:{self.server_port}")
        self._connect_to_server()

        # 启动独立的网络监听和播放线程
        self.network_thread = threading.Thread(target=self.network_listener_worker, daemon=True, name="NetworkListener")
        self.play_audio_thread = threading.Thread(target=self.play_audio_worker, daemon=True, name="AudioPlayer")
        self.network_thread.start()
        self.play_audio_thread.start()

    def log(self, message):
        print(f"[TTSClient] {message}")

    def _get_next_seq(self):
        self.seq += 1
        return self.seq

    def _split_text(self, text, max_length=100):
        sentences = [s.strip() for s in re.split(r'[？！。.~]\s*', text) if s.strip()]
        parts = []
        current_part = ""
        for sentence in sentences:
            if len(current_part) + len(sentence) + 1 <= max_length:
                current_part += sentence + "。"
            else:
                parts.append(current_part.strip())
                current_part = sentence + "。"
        if current_part: parts.append(current_part.strip())
        return parts

    def _filter_sensitive(self, text):
        for word in self.sensitive_words:
            text = text.replace(word, "**")
        return text

    def _send_request(self, payload):
        with self.lock:
            if not self.sock and not self._connect_to_server():
                return
            try:
                request_data = json.dumps(payload).encode('utf-8')
                self._send_msg(request_data)
            except Exception as e:
                self.log(f"发送请求时出错: {e}")
                self.sock = None

    def add_task(self, text, priority=2):
        if text in self.sounds_library:
            self._queue_local_sound(text, priority)
            return

        filtered_text = self._filter_sensitive(text)
        
        # 长文本分割逻辑
        sentences = []
        if len(filtered_text) > 100 and priority == 2: # 只有低优先级长文本才分割
             sentences = self._split_text(filtered_text)
        else:
             sentences = [filtered_text]

        for sentence in sentences:
            clean_sentence = sentence.replace(" ", "").replace("\n", "")
            if not clean_sentence: continue

            seq = self._get_next_seq()
            
            # 更新任务计数器
            if priority < 2: self.pending_priority1 += 1
            else: self.pending_priority2 += 1

            if priority == 1: # 自动任务需要跟踪ID以便取消
                self.auto_task_ids.append(seq)
                if len(self.auto_task_ids) > 5:
                    self.cancelled_auto_tasks.add(self.auto_task_ids.popleft())

            # 初始化重组缓冲区
            self.reassembly_buffer[seq] = {"main": None, "assist": None, "content": clean_sentence, "priority": priority}

            # 发送主声音请求
            main_payload = {"request_id": seq, "text": clean_sentence, "is_assistant": False}
            threading.Thread(target=self._send_request, args=(main_payload,)).start()

            # 关键词检测与助播逻辑
            triggered_keyword = next((kw for kw in self.keyword_responses if kw in clean_sentence), None)
            if triggered_keyword and self.ai_generator:
                self.log(f"🎤 检测到关键词 '{triggered_keyword}'，正在调用AI助播...")
                assistant_prompt = f"主播刚刚说了：『{clean_sentence}』。请你作为搭档，围绕关键词 '{triggered_keyword}'，说一句简短捧哏的话。"
                try:
                    assist_text = self.ai_generator.get_response("主播", assistant_prompt, [], is_assistant_task=True)
                    if assist_text:
                        self.log(f"🤖 AI助播生成内容: {assist_text}")
                        assist_payload = {"request_id": seq, "text": assist_text, "is_assistant": True}
                        threading.Thread(target=self._send_request, args=(assist_payload,)).start()
                    else:
                        # 如果AI没返回内容，也要标记助播部分已“完成”
                        self.reassembly_buffer[seq]['assist'] = "done"
                except Exception as e:
                    self.log(f"❌ 调用AI助播时出错: {e}")
                    self.reassembly_buffer[seq]['assist'] = "done"
            else:
                # 如果没有关键词，直接标记助播部分已“完成”
                self.reassembly_buffer[seq]['assist'] = "done"

    def network_listener_worker(self):
        """持续监听并接收服务器返回的音频块"""
        while not self._stop_event.is_set():
            if not self.sock: time.sleep(1); continue
            try:
                response_data = self._recv_msg()
                if response_data is None: self.sock = None; continue
                
                packet = json.loads(response_data.decode('utf-8'))
                req_id = packet.get('request_id')
                
                if not req_id or req_id not in self.reassembly_buffer: continue

                if packet.get("status") == "error":
                    self.log(f"收到服务器错误包: ReqID {req_id}")
                    # 标记对应的部分为失败
                    part = 'assist' if packet.get('is_assistant') else 'main'
                    self.reassembly_buffer[req_id][part] = "error"
                else:
                    audio_data = base64.b64decode(packet['audio_data'])
                    part = 'assist' if packet.get('is_assistant') else 'main'
                    self.reassembly_buffer[req_id][part] = audio_data
                
                # 检查这个任务的所有部分是否都已返回
                self._check_and_assemble(req_id)

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                self.log("与服务器连接中断..."); self.sock = None
            except Exception as e:
                self.log(f"网络监听线程出错: {e}")

    def _check_and_assemble(self, seq):
        """检查任务的所有部分是否都已收到，如果是，则拼接并入队"""
        buffer_entry = self.reassembly_buffer.get(seq)
        if buffer_entry and buffer_entry['main'] is not None and buffer_entry['assist'] is not None:
            # 所有部分都已收到（成功、失败或标记完成）
            buffer_entry = self.reassembly_buffer.pop(seq)
            
            main_data = buffer_entry['main']
            assist_data = buffer_entry['assist']
            
            if main_data == "error": # 如果主声音失败，则整个任务失败
                self.log(f"任务 {seq} 因主声音生成失败而被丢弃。")
                return

            try:
                # 保存主声音
                main_audio_path = os.path.join(self.output_dir, f"main_{seq}.wav")
                with open(main_audio_path, 'wb') as f: f.write(main_data)
                
                final_audio_path = main_audio_path
                
                # 如果有助播声音，则拼接
                if isinstance(assist_data, bytes):
                    assist_audio_path = os.path.join(self.output_dir, f"assist_{seq}.wav")
                    with open(assist_audio_path, 'wb') as f: f.write(assist_data)
                    
                    main_audio = AudioSegment.from_file(main_audio_path)
                    assist_audio = AudioSegment.from_file(assist_audio_path)
                    combined = main_audio + assist_audio
                    
                    final_audio_path = os.path.join(self.output_dir, f"combined_{seq}.wav")
                    combined.export(final_audio_path, format="wav")
                    
                    os.remove(assist_audio_path)
                    os.remove(main_audio_path)
                
                self.play_queue.put((buffer_entry["priority"], seq, final_audio_path, buffer_entry["content"]))
                self.log(f"✅ 任务 {seq} 处理完成并放入播放队列。")

            except Exception as e:
                self.log(f"❌ 拼接或保存任务 {seq} 音频时失败: {e}")

    def play_audio_worker(self):
        """【核心恢复】完整复刻 tts_text.py 的播放逻辑"""
        while not self._stop_event.is_set():
            try:
                priority, seq, audio_path, content = self.play_queue.get(timeout=1)
                
                with self.play_lock:
                    if priority == 1 and seq in self.cancelled_auto_tasks:
                        self.log(f"任务 {seq} 已被取消，跳过播放。")
                        self.play_queue.task_done()
                        self.pending_priority1 = max(0, self.pending_priority1 - 1)
                        if os.path.exists(audio_path): os.remove(audio_path)
                        continue

                    if self.now_playing_callback:
                        self.now_playing_callback(content)

                    self.log(f"正在播放任务 {seq} (Prio:{priority}): '{content[:50]}...'")
                    self._play_audio_in_subprocess(audio_path)
                    
                    if os.path.exists(audio_path) and not audio_path.startswith(self.sounds_path):
                        try: os.remove(audio_path)
                        except OSError: pass
                    
                    # 更新任务计数器
                    if priority < 2: self.pending_priority1 = max(0, self.pending_priority1 - 1)
                    else: self.pending_priority2 = max(0, self.pending_priority2 - 1)

                self.play_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                self.log(f"❌ 音频播放工作线程出错: {e}")

    def _queue_local_sound(self, command, priority):
        if self.now_playing_callback: self.now_playing_callback(command)
        sound_info = self.sounds_library.get(command)
        if not sound_info: return
        filename = random.choice(sound_info) if isinstance(sound_info, list) else sound_info
        audio_path = os.path.join(self.sounds_path, filename)
        if os.path.exists(audio_path):
            seq = self._get_next_seq()
            self.play_queue.put((priority, seq, audio_path, command))
            self.log(f"✅ 本地音效已入队: {command}")
        else:
            self.log(f"❌ 本地音效文件未找到: {audio_path}")

    # ... 其他辅助函数 ...
    def interrupt_and_speak(self, text):
        self.log(f"⚡ 收到紧急插话指令: {text}")
        self.add_task(text, priority=0)
    def get_unprocessed_size(self):
        return self.pending_priority1 + self.pending_priority2
    def can_generate_new_script(self):
        return self.get_unprocessed_size() < 5
    def test_and_play_sync(self, text, use_assistant=False):
        self.log(f"【声音测试】{'助播' if use_assistant else '主线'}: {text}")
        if not text: return
        request_type = 'test_assistant' if use_assistant else 'test_main'
        self.add_task(text, priority=-1, request_type=request_type)
    def stop(self):
        self._stop_event.set()
        if self.current_playback_process and self.current_playback_process.poll() is None:
            self.current_playback_process.terminate()
        with self.lock:
            if self.sock: self.sock.close(); self.sock = None
    def _play_audio_in_subprocess(self, audio_path):
        if self.current_playback_process and self.current_playback_process.poll() is None:
            self.current_playback_process.terminate()
            try: self.current_playback_process.wait(timeout=0.5)
            except subprocess.TimeoutExpired: pass
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            player_script_path = os.path.join(base_dir, "local_model_client.py")
            if not os.path.exists(player_script_path):
                self.log(f"❌ 致命错误：找不到播放脚本 'local_model_client.py'"); return
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
                self.log("✅ 成功连接到TTS服务器。"); return True
            except Exception as e:
                self.log(f"❌ 连接TTS服务器失败: {e}"); self.sock = None; return False
    def _send_msg(self, data):
        length = struct.pack('>I', len(data)); self.sock.sendall(length + data)
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
