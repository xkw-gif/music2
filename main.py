import random
import time
import requests

model1 = """欢迎第一次来到直播间的宝贝，有没有第一次来到主播直播间？
不知道直播间是一个什么样的直播间的宝贝
那么主播给你介绍一下，今天主播的话是作为一个破价福利直播间，
（产品名称）原价是卖到（产品名称+规格），
那么今天主播直播间给你们做一波福利，现在来到主播直播间的宝贝
原价（产品名称+价格），现在到手的话只需要（直播间价格）米就可以带回家了。一单就可以节约（优惠的钱）米。
这款（产品名称）的话在某猫官方旗舰店也是常年占据去（产品的领域：例如护肤等）领域销量榜首的，一年的话也是能卖到几百万单
所以说，宝贝不好用的产品的话，会有那么多个人去买嘛，不会的
而且今天主播直播间的话不仅给你做到优惠（优惠的价格）米的价格，拍了的宝贝可以回来打已拍，打了已拍的宝贝，主播的话给你备注一下，安排快马加鞭发货，所以说没拍没买的宝贝抓紧时间了，领取一下优惠券。抓经时间去拍，而且今天拍的宝子还送（赠品名称：如果没有就不说这句）"""

model2 = """今天拍下来的宝贝，大概今天就可以发货啦
有没有正在拍1号链接的宝贝， 你是不是在犹豫，纠结呢？  主播给各位宝贝说下，因为主播直播间真的很实惠，有没有买过的宝贝，咱们之前买一只是多少钱，是不是（官方旗舰店的价格）米，但是今天主播直播间只要（直播间的价格）米 ，有没有宝贝是不是担心这么一个优惠的价格，是不是正品，主播给宝贝们说一下，宝贝们可以看下付款页面，咱们这边是不是官方旗舰店的。宝贝，也不用担心日期的问题，都是新鲜日期。
所以说没拍没买的宝贝一定要抓紧时间了 咱家所剩下的库存不多了  一定要抓紧时间了 拍一单少一单优惠券所剩时间不多了 ，抓紧时间下单了 ，马上也要恢复原价了今天错过就没有啦
这是主播给你们的补贴才能得到的优惠哈。今天错过啦这样的优惠哈，我们在想拍，在想买的话是真的找不到的哈。所以没拍没买的姐妹抓紧时间啦，错过啦话，我们在想以这样的价格，这样的条件买到这样的产品是真的找不到啦（售后服务）"""

model3 = """如果可以的话，各位宝贝可以给主播点个小关小注，
因为主播直播间的产品的话还是比较实惠的，以后你们有想要任何产品的话；可以给主播点完关注过后
，过后我们可以先到主播直播间，来，先来对比一下价格，因为今天在主播直播间的话
我们拍一单（产品名称）的话就可以节省（节约的钱）米，拍两单（产品名称）的话可以节省（优惠的价格*2），拍三单的话可以立省（优惠的价格*3），我们谁的钱的话，也都不是大风刮来的。都是自己辛辛苦苦挣来的。
所以说能节省一点是一点，产品质量又是一样的。所以说喜欢主播的话，可以给主播点个小关小注，以后有想要产品的话都可以到主播直播间来看一下哈，很感谢宝贝的关注哦。
如果说你在主播直播间，没有找到你想要的产品的话，你也可以把你想的产品直接打在公屏上，主播看到过后都会尽可能的去给各位姐妹找找看哦"""

model = [model1, model2, model3]

class DeepSeekClient:
    def __init__(self, api_url, api_key):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.messages = [
            {"role": "system", "content": "你是个直播话术组手，我给你产品信息，并且给你话术模板，根据我给的信息，来适当更改。（）里面的内容，直接换，把元换成米。只回答话术，不回答别的。每次生成的话术，都不要一样，尽可能的改变一点"}
        ]

    def fetch_text(self, product_description):
        print("🎤 调用 DeepSeek API 生成文本中...")
        max_attempts = 5
        attempt = 0
        while attempt < max_attempts:
            try:
                # 添加随机话术模板
                self.messages.append({"role": "user", "content": f"话术模板{random.choice(self.model)}"})
                # 添加产品描述
                self.messages.append({"role": "user", "content": f"请根据以下产品信息:\n{product_description}"})
                response = requests.post(
                    f"{self.api_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "deepseek-chat",
                        "messages": self.messages,
                        "stream": False,
                        "temperature": 1.8,
                        "top_p": 0.7
                    }
                )
                response.raise_for_status()
                result = response.json()
                speech = result["choices"][0]["message"]["content"]
                self.messages.append({"role": "assistant", "content": speech})
                print(f"✅ DeepSeek 生成内容: {speech}")
                return speech
            except requests.HTTPError as e:
                if response.status_code == 400:
                    attempt += 1
                    wait_time = 10 * attempt
                    print(f"❌ 请求返回 400，重试 {attempt}/{max_attempts}，等待 {wait_time} 秒...")
                    time.sleep(wait_time)
                    # 当消息列表超过 20 条时，重置对话历史，防止消息累积
                    if len(self.messages) > 20:
                        print("🔄 消息累计过多，重置对话历史")
                        self.messages = [{
                            "role": "system",
                            "content": "你是个直播话术组手，我给你产品信息，并且给你话术模板，根据我给的信息，来适当更改。（）里面的内容，直接换，把元换成米。只回答话术，不回答别的。每次生成的话术，都不要一样，尽可能的改变一点"
                        }]
                else:
                    print(f"❌ DeepSeek 生成失败: {e}")
                    return ""
            except Exception as e:
                print(f"❌ DeepSeek 生成失败: {e}")
                return ""
        print("❌ 达到最大重试次数，生成失败")
        return ""
