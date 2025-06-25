import requests
import time
import os

# ====== 配置部分 ======
BARK_API_KEY = os.getenv('BARK_API_KEY', 'Znodd8yskndqUUbMVnmzBn')  # 建议用GitHub Secrets设置
BARK_API_URL = f'https://api.day.app/{BARK_API_KEY}/'
ALERT_PRICE = os.getenv('ALERT_PRICE')  # 可以设置为具体价格
USE_MA200 = os.getenv('USE_MA200', 'false').lower() == 'true'  # 是否用200日均线
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '60'))  # 检查频率（秒）

# ====== 获取BTC现价函数 ======
def get_btc_price():
    try:
        url = 'https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT'
        resp = requests.get(url, timeout=5)
        data = resp.json()
        return float(data['price'])
    except Exception as e:
        print(f'获取价格失败: {e}')
        return None

# ====== 获取BTC 200日均线 ======
def get_btc_ma200():
    try:
        url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=200'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item[4]) for item in data]  # 收盘价
        ma200 = sum(closes) / len(closes)
        return ma200
    except Exception as e:
        print(f'获取MA200失败: {e}')
        return None

# ====== 发送Bark推送 ======
def send_bark_alert(price, threshold):
    title = 'BTC价格预警'
    body = f'BTC价格${price:.2f}已跌破阈值${threshold:.2f}'
    url = f"{BARK_API_URL}{title}/{body}"
    try:
        requests.get(url)
        print('已发送推送通知')
    except Exception as e:
        print(f'推送失败: {e}')

# ====== 主循环 ======
if __name__ == '__main__':
    if USE_MA200:
        threshold = get_btc_ma200()
        print(f'使用200日均线作为阈值: ${threshold}')
    else:
        threshold = float(ALERT_PRICE) if ALERT_PRICE else 60000
        print(f'使用固定阈值: ${threshold}')
    while True:
        price = get_btc_price()
        if price is not None:
            print(f'当前BTC价格：${price}')
            if price < threshold:
                send_bark_alert(price, threshold)
                break  # 只推送一次，推送后退出。如需持续推送可去掉这行
        time.sleep(CHECK_INTERVAL) 