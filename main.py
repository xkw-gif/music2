# tts_client.py (V9.0 - 最终完整重构版)
# 核心架构：完整复刻原 tts_text.py 的所有功能逻辑，包括任务跟踪、文本预处理、
# 音频块重组等，仅将本地语音合成替换为对服务器的网络请求。
import collections, random, re, time, os, shutil, threading, queue, json, struct, socket, sys, base64
import subprocess
from pydub import AudioSegment

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
        
        # 【核心恢复】任务跟踪与重组机制
        self.play_queue = queue.PriorityQueue()
        self.reassembly_buffer = {} # 音频块重组缓冲区
        self.pending_requests = set() # 跟踪所有已发送但未完成的请求ID
        
        self._stop_event = threading.Event()
        self.current_playback_process = None
        self.request_id_counter = 0

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
        sentences = [s.strip() for s in re.split(r'([？！。.~…\n，,])', text) if s.strip()]
        merged = []
        temp = ""
        for item in sentences:
            if item in '？！。.~…\n，,':
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
                # 如果发送失败，需要从待处理集合中移除
                self.pending_requests.discard(payload['request_id'])
                return
            try:
                request_data = json.dumps(payload).encode('utf-8')
                self._send_msg(request_data)
            except Exception as e:
                self.log(f"发送音频块请求时出错: {e}")
                self.pending_requests.discard(payload['request_id'])
                self.sock = None

    def add_task(self, text, priority=2, request_type='default'):
        """【核心重构】完整复刻 tts_text.py 的文本预处理和任务分发逻辑"""
        if text in self.sounds_library:
            self._queue_local_sound(text, priority)
            return

        filtered_text = self._filter_sensitive(text)
        request_id = self._get_next_request_id()
        self.pending_requests.add(request_id) # 【核心恢复】开始跟踪这个新任务

        chunks = []
        if len(filtered_text) > 100:
            chunks = self._split_text(filtered_text)
        else:
            chunks = [filtered_text]

        if not chunks: 
            self.pending_requests.discard(request_id)
            return
            
        total_chunks = len(chunks)
        self.log(f"新任务 ReqID:{request_id} (Prio:{priority}) 被分割成 {total_chunks} 块，开始发送请求...")

        # 初始化重组缓冲区
        self.reassembly_buffer[request_id] = {
            "chunks": {}, "total": total_chunks, "priority": priority, "full_text_map": {}
        }

        for i, chunk_text in enumerate(chunks):
            chunk_id = i + 1
            clean_chunk = chunk_text.replace(" ", "").replace("\n", "")
            if not clean_chunk:
                # 如果块为空，需要特殊处理以避免死锁
                self.reassembly_buffer[request_id]['total'] -= 1
                if self.reassembly_buffer[request_id]['total'] == 0:
                    self.reassembly_buffer.pop(request_id, None)
                    self.pending_requests.discard(request_id)
                continue

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
        """【核心恢复】准确计算正在处理和等待播放的任务总数"""
        return self.play_queue.qsize() + len(self.pending_requests)

    def can_generate_new_script(self):
        """【核心恢复】准确判断是否可以生成新话术"""
        return self.get_unprocessed_size() < 5

    def test_and_play_sync(self, text, use_assistant=False):
        self.log(f"【声音测试】{'助播' if use_assistant else '主线'}: {text}")
        if not text: return
        request_type = 'test_assistant' if use_assistant else 'test_main'
        self.add_task(text, priority=-1, request_type=request_type)

    def network_listener_worker(self):
        """持续监听并接收服务器返回的音频块"""
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
                
                req_id = packet.get('request_id')
                if not req_id or req_id not in self.reassembly_buffer:
                    continue # 忽略无效或过时的数据包

                if packet.get("status") == "error":
                    self.log(f"收到服务器错误包: ReqID {req_id}, Chunk {packet.get('chunk_id')}")
                    # 即使块失败，也要计入，以防死锁
                    self.reassembly_buffer[req_id]["chunks"][packet['chunk_id']] = None 
                else:
                    audio_data = base64.b64decode(packet['audio_data'])
                    self.reassembly_buffer[req_id]["chunks"][packet['chunk_id']] = (audio_data, packet['text'])
                
                buffer_entry = self.reassembly_buffer[req_id]
                # 检查是否已收齐所有块（包括失败的）
                if len(buffer_entry["chunks"]) >= buffer_entry["total"]:
                    self.log(f"ReqID {req_id} 的所有 {buffer_entry['total']} 个音频块已收齐，准备拼接。")
                    self._assemble_and_queue_audio(req_id)

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                self.log("与服务器连接中断，将尝试重连...")
                self.sock = None
            except Exception as e:
                self.log(f"网络监听线程出错: {e}")

    def _assemble_and_queue_audio(self, req_id):
        """拼接指定ID的音频块并放入播放队列"""
        buffer_entry = self.reassembly_buffer.pop(req_id, None)
        if not buffer_entry: return
        
        try:
            combined_audio = AudioSegment.empty()
            full_text_parts = []
            
            for i in range(1, buffer_entry['total'] + 1):
                chunk_data = buffer_entry['chunks'].get(i)
                if chunk_data: # 跳过失败的块
                    audio_data, text_part = chunk_data
                    temp_chunk_path = os.path.join(self.output_dir, f"chunk_{req_id}_{i}.wav")
                    with open(temp_chunk_path, 'wb') as f:
                        f.write(audio_data)
                    
                    segment = AudioSegment.from_file(temp_chunk_path)
                    combined_audio += segment
                    full_text_parts.append(text_part)
                    os.remove(temp_chunk_path)

            if len(combined_audio) > 0:
                final_path = os.path.join(self.output_dir, f"final_audio_{req_id}.wav")
                combined_audio.export(final_path, format="wav")
                
                full_text = "".join(full_text_parts)
                self.play_queue.put((buffer_entry["priority"], req_id, final_path, full_text))
                self.log(f"✅ ReqID {req_id} 音频拼接完成并放入播放队列。")

        except Exception as e:
            self.log(f"❌ 拼接 ReqID {req_id} 音频时失败: {e}")
        finally:
            # 【核心恢复】无论成功与否，都要结束对这个任务的跟踪
            self.pending_requests.discard(req_id)


    def play_audio_worker(self):
        while not self._stop_event.is_set():
            try:
                priority, seq, audio_path, content = self.play_queue.get(timeout=1)
                
                if self.now_playing_callback:
                    self.now_playing_callback(content)
                
                self.log(f"正在播放任务 Seq/ReqID:{seq} (Prio:{priority}): '{content[:50]}...'")
                self._play_audio_in_subprocess(audio_path)
                
                if os.path.exists(audio_path) and not audio_path.startswith(self.sounds_path):
                    try: os.remove(audio_path)
                    except OSError: pass
                
                self.play_queue.task_done()
            except queue.Empty:
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
            self.play_queue.put((priority, req_id, audio_path, command))
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
