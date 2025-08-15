# tts_client.py (V6.0 - 最终稳定版)
# 核心架构：回归稳定的请求-响应模式，客户端发送请求，服务器返回处理好的完整音频。
# 本次更新：恢复所有功能，确保与新版服务器完美匹配。
import collections, random, re, time, os, shutil, threading, queue, json, struct, socket, sys, base64
import subprocess
from pydub import AudioSegment

class TTSClientGenerator:
    def __init__(self, server_host, server_port, output_dir, **kwargs):
        self.server_host = server_host
        self.server_port = server_port
        self.sock = None
        self.lock = threading.Lock() # 锁，用于保护socket连接
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.sounds_path = "sounds"
        self.sounds_library = {"[咳嗽]": "咳嗽声.WAV", "[叹气]": "叹气声.WAV", "[吞咽]": "吞咽声.WAV",
                               "[呼吸]": ["呼吸1.WAV", "呼吸2.WAV", "呼吸3.WAV"]}
        
        self.keyword_responses = kwargs.get('keyword_responses', {})
        
        self.play_queue = queue.PriorityQueue()
        self._stop_event = threading.Event()
        self.current_playback_process = None
        self.seq = 0 # 请求序列号

        self.log(f"TTS客户端初始化，准备连接服务器 {self.server_host}:{self.server_port}")
        self._connect_to_server()

        self.play_audio_thread = threading.Thread(target=self.play_audio_worker, daemon=True)
        self.play_audio_thread.start()

    def log(self, message):
        print(f"[TTSClient] {message}")

    def _send_request_to_server(self, payload):
        """统一的网络请求发送函数，在独立的线程中执行"""
        def task():
            with self.lock:
                if not self.sock and not self._connect_to_server():
                    self.log(f"任务发送失败，无法连接服务器。")
                    return
                try:
                    # 1. 发送请求
                    request_data = json.dumps(payload).encode('utf-8')
                    self._send_msg(request_data)
                    self.log(f"已发送请求 (类型:{payload['request_type']}): '{payload['text'][:30]}...'")

                    # 2. 等待并接收完整的音频文件
                    audio_data = self._recv_msg()
                    if not audio_data or len(audio_data) < 100:
                        # 检查是否是错误信息
                        try:
                            error_info = json.loads(audio_data.decode('utf-8'))
                            if 'error' in error_info:
                                self.log(f"❌ 服务器处理任务时返回错误: {error_info['error']}")
                                return
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            raise ConnectionError("服务器返回数据无效或为空。")
                    
                    # 3. 保存音频文件并放入播放队列
                    self.seq += 1
                    timestamp = int(time.time())
                    new_audio_path = os.path.join(self.output_dir, f"audio_{timestamp}_{self.seq}.wav")
                    with open(new_audio_path, 'wb') as f:
                        f.write(audio_data)
                    
                    self.play_queue.put((payload['priority'], self.seq, new_audio_path, payload['text']))
                    self.log(f"✅ 已接收并入队音频 (Prio:{payload['priority']})")

                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as e:
                    self.log(f"❌ 与服务器连接中断: {e}。")
                    self.sock = None
                except Exception as e:
                    self.log(f"❌ 处理网络任务时发生未知错误: {e}")
                    self.sock = None
        
        # 每个请求都在一个新线程中处理，防止阻塞
        threading.Thread(target=task, daemon=True).start()

    def add_task(self, text, priority=2):
        """主程序调用的唯一入口：发送常规任务请求到服务器"""
        if text in self.sounds_library:
            self._queue_local_sound(text, priority)
            return
        
        triggered_keyword = next((kw for kw in self.keyword_responses if kw in text), None)
        
        payload = {
            "text": text,
            "priority": priority,
            "request_type": "default",
            "triggered_keyword": triggered_keyword
        }
        self._send_request_to_server(payload)

    def interrupt_and_speak(self, text):
        """紧急插话功能"""
        self.log(f"⚡ 收到紧急插话指令: {text}")
        self.add_task(text, priority=0)

    def get_unprocessed_size(self):
        """返回等待播放的任务总数"""
        return self.play_queue.qsize()

    def can_generate_new_script(self):
        """判断是否可以生成新话术"""
        return self.get_unprocessed_size() < 5

    def test_and_play_sync(self, text, use_assistant=False):
        """发送专门的测试请求"""
        self.log(f"【声音测试】{'助播' if use_assistant else '主线'}: {text}")
        if not text: return
        
        payload = {
            "text": text,
            "priority": -1,
            "request_type": 'test_assistant' if use_assistant else 'test_main'
        }
        self._send_request_to_server(payload)
        self.log("【声音测试】请求已发送到服务器，请等待播放...")

    def play_audio_worker(self):
        while not self._stop_event.is_set():
            try:
                priority, seq, audio_path, content = self.play_queue.get(timeout=1)
                self.log(f"正在播放任务 Seq:{seq} (Prio:{priority}): '{content[:50]}...'")
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
        sound_info = self.sounds_library.get(command)
        if not sound_info: return
        filename = random.choice(sound_info) if isinstance(sound_info, list) else sound_info
        audio_path = os.path.join(self.sounds_path, filename)
        if os.path.exists(audio_path):
            self.seq += 1
            self.play_queue.put((priority, self.seq, audio_path, command))
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
