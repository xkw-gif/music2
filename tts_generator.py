import time
import os
import shutil
import threading
import queue
from collections import deque
from pydub import AudioSegment
from pydub.playback import play
from gradio_client import Client, handle_file
from concurrent.futures import ThreadPoolExecutor

class TTSGenerator:
    def __init__(self, client_url, ref_audio_path, output_dir):
        self.client = Client(client_url)
        self.ref_audio_path = ref_audio_path
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self.number = 0

        # 音频任务队列和播放记录
        self.audio_queue = queue.PriorityQueue()
        self.played_queue = []  # 改为列表，保存所有播放过的音频
        self.low_priority_queue = deque(maxlen=2)  # 记录优先级低的音频路径
        self.high_priority_queue = deque(maxlen=2)  # 记录优先级高的音频路径
        self.played_audio_paths = set()  # 记录已播放的音频路径

        # 锁机制确保音频播放同步
        self.play_lock = threading.Lock()

        # 删除原来的统一线程池，添加两个专用线程池
        self.executor_high = ThreadPoolExecutor(max_workers=5)
        self.executor_low = ThreadPoolExecutor(max_workers=5)

        # 启动播放线程
        self.play_audio_thread = threading.Thread(target=self.play_audio_worker, daemon=True)
        self.play_audio_thread.start()

    def generate_audio(self, text, priority):
        self.number = self.number + 1

        def task():
            try:
                result = self.client.predict(
                    ref_wav_path=handle_file(self.ref_audio_path),
                    prompt_text="十分钟温水冲泡三秒之内喝掉啊，饱腹感达到四到六个小时的啊",
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
                print(f"这是优先级{priority}的音频文件")

                if priority == 1:
                    self.high_priority_queue.append(new_audio_path)
                else:
                    self.low_priority_queue.append(new_audio_path)
            except Exception as e:
                print(f"❌ 语音合成出错: {e}")

        # 根据优先级提交任务到对应线程池
        if priority == 1:
            self.executor_high.submit(task)
        else:
            self.executor_low.submit(task)

    def play_audio_worker(self):
        while True:
            with self.play_lock:
                try:
                    if self.high_priority_queue:
                        audio_path = self.high_priority_queue.popleft()
                    elif self.low_priority_queue:
                        audio_path = self.low_priority_queue.popleft()
                    else:
                        continue  # 没有音频播放时继续循环

                    audio = AudioSegment.from_file(audio_path)
                    play(audio)
                    self.played_audio_paths.add(audio_path)
                    print(f"还有{self.number}个音频未生成音频")
                    self.number = self.number - 1

                    print(f"🔊 播放完成: {audio_path}")

                    # 删除已播放的音频文件
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                        self.played_audio_paths.remove(audio_path)
                except Exception as e:
                    print(f"❌ 音频播放失败: {e}")

    def add_task(self, text, priority=2):
        self.generate_audio(text, priority)

    def get_unprocessed_size(self):
        return self.audio_queue.qsize()

    def wait_for_completion(self):
        self.audio_queue.join()

    def get_number_ds(self):
        return self.number

    def shutdown(self):
        # 修改关闭，需同时关闭两个线程池
        self.executor_high.shutdown(wait=True)
        self.executor_low.shutdown(wait=True)
        self.play_audio_thread.join()

    def can_generate_new_script(self):
        return len(self.low_priority_queue) < 2 and len(self.high_priority_queue) < 2
