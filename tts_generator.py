import time
import os
import shutil
import threading
import queue
from collections import deque
from pydub import AudioSegment
from pydub.playback import play, _play_with_simpleaudio
from gradio_client import Client, handle_file
from concurrent.futures import ThreadPoolExecutor

class TTSGenerator:
    def __init__(self, client_url, so_vits_path, gpt_path, ref_audio_path, output_dir):
        self.client = Client(client_url)
        self.so_vits_path = so_vits_path
        self.gpt_path = gpt_path
        self.ref_audio_path = ref_audio_path
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        # 音频任务队列和播放记录
        self.audio_queue = queue.PriorityQueue()
        self.played_queue = deque(maxlen=2)  # 仅保留最近两轮音频

        # 锁机制确保音频播放同步
        self.play_lock = threading.Lock()

        # 线程池管理任务
        self.executor = ThreadPoolExecutor(max_workers=10)

        # 启动播放线程
        threading.Thread(target=self.play_audio_worker, daemon=True).start()

        self.currently_playing = None  # 当前播放的音频对象
        self.currently_playing_path = None  # 当前播放的音频路径
        self.stop_event = threading.Event()  # 用于停止播放的事件

    def generate_audio(self, text, priority):
        def task():
            try:
                result = self.client.predict(
                    ref_wav_path=handle_file(self.ref_audio_path),
                    prompt_text="蜜瓜椰椰味，或者咱们的杨枝甘露味道就可以了。0~3岁，宝宝们。",
                    prompt_language="中文",
                    text=text,
                    text_language="中文",
                    how_to_cut="凑四句一切",
                    top_k=15,
                    top_p=1,
                    temperature=1,
                    ref_free=False,
                    speed=0.85,
                    if_freeze=False,
                    inp_refs=None,
                    sample_steps=8,
                    if_sr=False,
                    pause_second=0.3,
                    api_name="/get_tts_wav"
                )

                if not result:
                    print("❌ 语音合成失败: API 未返回有效数据")
                    return

                output_audio_path = result
                timestamp = int(time.time())
                new_audio_path = os.path.join(self.output_dir, f"audio_{timestamp}.wav")

                # 复制文件确保稳定性
                shutil.copy2(output_audio_path, new_audio_path)
                os.remove(output_audio_path)  # 删除临时文件
                print(f"✅ 语音合成完成: {new_audio_path}")

                self.audio_queue.put((priority, new_audio_path, text))
            except Exception as e:
                print(f"❌ 语音合成出错: {e}")

        self.executor.submit(task)

    def play_audio_worker(self):
        while not self.stop_event.is_set():
            try:
                priority, audio_path, text = self.audio_queue.get(timeout=1)
            except queue.Empty:
                continue

            with self.play_lock:
                try:
                    if self.currently_playing:
                        self.currently_playing.stop()  # 停止当前播放
                        if self.currently_playing_path and os.path.exists(self.currently_playing_path):
                            os.remove(self.currently_playing_path)  # 删除当前播放的音频文件
                        self.currently_playing = None
                        self.currently_playing_path = None

                    audio = AudioSegment.from_file(audio_path)
                    self.currently_playing = _play_with_simpleaudio(audio)  # 播放新音频
                    self.currently_playing_path = audio_path
                    self.played_queue.append(audio_path)

                    print(f"🔊 播放完成: {text}")

                    # 只保留最近 2 轮音频，删除旧音频
                    while len(self.played_queue) > 2:
                        old_audio_path = self.played_queue.popleft()
                        if os.path.exists(old_audio_path):
                            os.remove(old_audio_path)
                except Exception as e:
                    print(f"❌ 音频播放失败: {e}")
                finally:
                    self.audio_queue.task_done()
                    self.currently_playing = None  # 重置当前播放对象
                    self.currently_playing_path = None  # 重置当前播放路径

    def add_task(self, text, priority=2):
        self.generate_audio(text, priority)

    def get_unprocessed_size(self):
        return self.audio_queue.qsize()

    def wait_for_completion(self):
        self.audio_queue.join()

    def shutdown(self):
        self.stop_event.set()
        self.executor.shutdown(wait=True)