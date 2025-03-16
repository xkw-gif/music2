import threading
import time
from liveMan import DouyinLiveWebFetcher
from tts_generator import TTSGenerator
from deepseek_client import DeepSeekClient
from ali import stream_chat_with_voice as st

# 创建 TTSGenerator 实例
deepseek = DeepSeekClient(
    api_url="https://api.deepseek.com/v1",
    api_key="sk-5e0c624b39cc4f5c8377a5a1009be2a3"
)

tts = TTSGenerator(
    client_url="http://localhost:9872/",
    ref_audio_path=r"C:\Users\徐康文\Desktop\样本声音\十分钟温水冲泡三秒之内喝掉啊，饱腹感达到四到六个小时的啊。.wav",
    output_dir=r"D:/output_audio"
)

# 产品描述（固定）
with open('product.txt', 'r', encoding='utf-8') as file:
     product_description = file.read()

def handle_new_comment(username, comment):
    print(f"回调函数获取到的最新评论: {username}: {comment}")
    # 将新评论任务添加到队列中，优先级为1
    tts.add_task(st(username,comment), priority=1)

def generate_script_task():
    while True:
        # 检查当前队列大小，确保不会生成过多的任务
        if tts.get_unprocessed_size() < 2 and tts.can_generate_new_script():
            #检查还没有生成语音的文本
            if tts.get_number_ds()<4:
            # 生成话术文本并添加到队列中，优先级为2
                tts.add_task(deepseek.fetch_text(product_description), priority=2)
        # 等待一段时间后再生成下一个话术文本
        time.sleep(10)  # 根据需要调整时间间隔

if __name__ == '__main__':
    live_id = '914411562232'
    room = DouyinLiveWebFetcher(live_id)

    # 设置回调函数
    room.comment_callback = handle_new_comment

    # 获取直播间状态
    room.get_room_status()

    # 启动任务处理线程
    threading.Thread(target=tts.play_audio_worker, daemon=True).start()

    # 启动话术生成任务线程
    script_thread = threading.Thread(target=generate_script_task, daemon=True)
    script_thread.start()

    # 启动 WebSocket 连接，开始抓取弹幕
    while True:
        try:
            room.start()
        except KeyboardInterrupt:
            room.stop()
            tts.shutdown()
            script_thread.join()
            break
        except Exception as e:
            print(f"❌ 连接出错: {e}")
            time.sleep(5)  # 等待一段时间后重试
