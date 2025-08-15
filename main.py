# tts_server.py (V5.0 - 流式处理版)
# 核心架构变更：服务器不再拼接音频，而是将长文本分割后，异步生成音频块，并立即流式返回给客户端。
import socket
import threading
import json
import logging
import os
import struct
import time
import random
import re
import requests
import base64 # 用于音频数据的编码
from gradio_client import Client, handle_file
from concurrent.futures import ThreadPoolExecutor # 引入线程池

# --- 服务器端配置 ---
GRADIO_URL = "http://localhost:9872/"
REF_AUDIO_PATH = "领红包左上角抢红包啦，领完红包，抢完红包，再去拍，再去卖，再去拍啊，正品保证正品保真啦。.wav"
# ... 其他配置保持不变 ...
ASSISTANT_TTS_URL = "http://localhost:9880/"
ASSISTANT_SPEAKER = "助播2.pt"
DASHSCOPE_API_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxx"
SERVER_HOST = '0.0.0.0'
SERVER_PORT = 12345
OUTPUT_DIR = "server_temp_audio"
LONG_TEXT_THRESHOLD = 100

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')

class StreamingTTSServer:
    def __init__(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self.main_tts_client = None
        self.request_id_counter = 0
        self.request_lock = threading.Lock()

        # 【核心改动】引入高、低优先级两个线程池
        self.executor_high = ThreadPoolExecutor(max_workers=5, thread_name_prefix='HighPrio')
        self.executor_low = ThreadPoolExecutor(max_workers=5, thread_name_prefix='LowPrio')

        try:
            self.main_tts_client = Client(GRADIO_URL)
            logging.info("✅ 主TTS服务连接成功。")
        except Exception as e:
            logging.error(f"❌ 无法连接主TTS服务: {e}")

    def get_next_request_id(self):
        with self.request_lock:
            self.request_id_counter += 1
            return self.request_id_counter

    def _split_sentences(self, text):
        # ... 分割逻辑不变 ...
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

    def _generate_and_send_chunk(self, client_socket, text_chunk, priority, request_id, chunk_id, total_chunks, is_assistant=False):
        """生成单个音频块并立即发送的核心函数"""
        try:
            audio_path = self.generate_assistant_audio(text_chunk) if is_assistant else self.generate_main_audio(text_chunk)
            
            if audio_path and os.path.exists(audio_path):
                with open(audio_path, 'rb') as f:
                    audio_data = f.read()
                
                # 使用Base64编码音频数据，确保JSON传输安全
                encoded_audio = base64.b64encode(audio_data).decode('utf-8')
                
                response_packet = {
                    "status": "success",
                    "request_id": request_id,
                    "chunk_id": chunk_id,
                    "total_chunks": total_chunks,
                    "priority": priority,
                    "text": text_chunk,
                    "audio_data": encoded_audio
                }
                self._send_msg(client_socket, json.dumps(response_packet).encode('utf-8'))
                logging.info(f"✅ 已发送音频块 {chunk_id}/{total_chunks} (ReqID: {request_id})")
                os.remove(audio_path)
            else:
                raise ValueError("音频文件生成失败或未找到")
        except Exception as e:
            logging.error(f"处理音频块 {chunk_id}/{total_chunks} (ReqID: {request_id}) 失败: {e}")
            # 发送一个失败的包，让客户端知道
            error_packet = {"status": "error", "request_id": request_id, "chunk_id": chunk_id, "message": str(e)}
            self._send_msg(client_socket, json.dumps(error_packet).encode('utf-8'))

    def handle_client(self, client_socket, addr):
        logging.info(f"接受来自 {addr} 的新连接。")
        try:
            while True:
                data = self._recv_msg(client_socket)
                if data is None: break

                message = json.loads(data.decode('utf-8'))
                text = message.get('text')
                priority = message.get('priority', 2) # 从客户端接收优先级

                request_id = self.get_next_request_id()
                logging.info(f"收到新任务 ReqID:{request_id}, Prio:{priority}, Text:'{text[:30]}...'")

                # 1. 分割文本
                text_chunks = self._split_sentences(text)
                if not text_chunks: continue
                total_chunks = len(text_chunks)

                # 2. 选择执行器并提交任务
                executor = self.executor_high if priority < 2 else self.executor_low
                
                for i, chunk in enumerate(text_chunks):
                    chunk_id = i + 1
                    executor.submit(
                        self._generate_and_send_chunk,
                        client_socket,
                        chunk,
                        priority,
                        request_id,
                        chunk_id,
                        total_chunks,
                        is_assistant=False # 简化：此示例中所有请求都为主播声音
                    )
        except (ConnectionResetError, json.JSONDecodeError):
            logging.warning(f"客户端 {addr} 连接异常或断开。")
        finally:
            client_socket.close()

    # ... generate_main_audio 和 generate_assistant_audio 函数保持不变 ...
    def generate_main_audio(self, text):
        if not self.main_tts_client: return None
        audio_path = None
        try:
            speed = round(random.uniform(0.9, 1.1), 2)
            audio_path = self.main_tts_client.predict(
                ref_wav_path=handle_file(REF_AUDIO_PATH), prompt_text="领红包左上角抢红包啦，领完红包，抢完红包，再去拍，再去卖，再去拍啊，正品保证正品保真啦。", text=text,
                prompt_language="中文", text_language="中文", how_to_cut="凑四句一切",
                top_k=15, top_p=1, temperature=1, ref_free=False, speed=speed, api_name="/get_tts_wav"
            )
            return audio_path
        except Exception as e:
            logging.error(f"主TTS生成失败: {e}")
            if audio_path and os.path.exists(audio_path): os.remove(audio_path)
            return None

    def generate_assistant_audio(self, text):
        params = {"text": text, "speaker": ASSISTANT_SPEAKER, "volume": 1.3, "speed": 1.1}
        temp_path = os.path.join(OUTPUT_DIR, f"assist_{int(time.time())}.wav")
        try:
            url_to_request = ASSISTANT_TTS_URL.strip('/')
            response = requests.get(url_to_request, params=params, stream=True, timeout=10)
            response.raise_for_status()
            with open(temp_path, 'wb') as f: f.write(response.content)
            return temp_path
        except Exception as e:
            logging.error(f"助播TTS生成失败：{e}")
            if os.path.exists(temp_path): os.remove(temp_path)
            return None

    # ... start() 和网络辅助函数保持不变 ...
    def start(self):
        if not self.main_tts_client:
            logging.critical("❌ 主TTS服务初始化失败，服务器无法启动。")
            return
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((SERVER_HOST, SERVER_PORT))
        server_socket.listen(5)
        logging.info(f"🚀 流式服务器已启动，正在监听 {SERVER_HOST}:{SERVER_PORT}...")
        try:
            while True:
                client, addr = server_socket.accept()
                threading.Thread(target=self.handle_client, args=(client, addr), daemon=True).start()
        except KeyboardInterrupt:
            logging.info("服务器正在关闭...")
        finally:
            server_socket.close()

    def _send_msg(self, sock, data):
        try:
            length = struct.pack('>I', len(data))
            sock.sendall(length + data)
        except (ConnectionResetError, BrokenPipeError):
            logging.warning("尝试向已关闭的客户端发送消息。")
    def _recv_msg(self, sock):
        raw_msglen = self._recv_all(sock, 4)
        if not raw_msglen: return None
        msglen = struct.unpack('>I', raw_msglen)[0]
        return self._recv_all(sock, msglen)
    def _recv_all(self, sock, n):
        data = bytearray()
        while len(data) < n:
            packet = sock.recv(n - len(data))
            if not packet: return None
            data.extend(packet)
        return data

if __name__ == "__main__":
    server = StreamingTTSServer()
    server.start()
