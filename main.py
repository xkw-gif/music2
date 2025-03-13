import threading
import time
from liveMan import DouyinLiveWebFetcher
from tts_generator import TTSGenerator
from deepseek_client import DeepSeekClient
from ali import stream_chat_with_voice as st
from ali2 import stream_chat_with_voice as st2

# 创建 TTSGenerator 实例
deepseek = DeepSeekClient(
    api_url="https://api.deepseek.com/v1",
    api_key="sk-5e0c624b39cc4f5c8377a5a1009be2a3"
)

tts = TTSGenerator(
    client_url="http://localhost:9872/",
    so_vits_path="D:/新建文件夹/GPT-SoVITS-v3lora-20250228/SoVITS_weights_v2/阿贝多_e10_s480.pth",
    gpt_path="D:/新建文件夹/GPT-SoVITS-v3lora-20250228/GPT_weights_v2/阿贝多-e10.ckpt",
    ref_audio_path=r"C:\Users\徐康文\Desktop\3月11日 (2).MP3",
    output_dir=r"D:/output_audio"
)
# 产品描述（固定）
product_description = """1.产品名称 荷馨防脱育发洗发水 2.核心卖点 ：防脱，温和，滋养头皮 3.价格对比（直播间价 vs 官方价，比如：249 vs 旗舰店299） 4.规格/数量（比如：两瓶洗发水350ml） 5.限时福利（比如：需要领取优惠券才能优惠：优惠券补贴好了 主播补贴价、赠品：20只精华液+3只发膜、库存限量） 6.信任保障（售后政策/发货渠道，比如：七天无理由/过敏退 + 官方旗舰店发货） 7.互动指令（让观众怎么做，比如：扣1/点购物车/赶紧下单）优惠券已经补贴好啦 主播补贴价、赠品：、库存限量） 6.信任保障（售后政策/发货渠道，比如：支持试用，不好用可以退，七天无理由/过敏退 + 官方旗舰店发货） 7.互动指令（让观众怎么做，比如：扣1/点购物车/赶紧下单）"""

def handle_new_comment(username, comment):
    if username != "Makeup. Fairt":
        print(f"回调函数获取到的最新评论: {username}: {comment}")
        # 将新评论任务添加到队列中，优先级为1
        tts.add_task(st(username, comment), priority=1)


def chat():
    """
    生成话术文本。

    :return: 话术文本
    """
    return "这是一个话术文本示例。"


def generate_script_task():
    while True:
        # 检查当前队列大小，确保不会生成过多的任务
        if tts.get_unprocessed_size() < 2:
            # 生成话术文本并添加到队列中，优先级为2
            tts.add_task( deepseek.fetch_text(product_description), priority=2)
        # 等待一段时间后再生成下一个话术文本
        time.sleep(10)  # 根据需要调整时间间隔


if __name__ == '__main__':
    live_id = '730402328052'
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
    try:
        room.start()
    except KeyboardInterrupt:
        room.stop()
        tts.shutdown()
        script_thread.join()
